"""
riskforge-execute-sql -- runs one read-only SELECT against RDS PostgreSQL.

The port of tools/sql_executor_tool.py. Two things changed on the way, both
because the destination is a Lambda behind an LLM rather than a local process:

1. **Where read-only comes from.** SQLite's `mode=ro` made writes physically
   impossible through the connection, whatever the query text claimed. There is
   no PostgreSQL equivalent, so the guarantee moved into the server: this
   function connects as `riskforge_ro`, which holds SELECT on three tables and
   nothing else (Deploy/lambda/sql/create_readonly_role.sql). The keyword filter
   below survives as a pre-filter that turns a rejected write into a clear
   message instead of a permission error, not as the boundary.

2. **Where the rows go.** The local tool returned an 878k-row DataFrame in
   memory. A Lambda cannot: the response payload caps at 6 MB, and more to the
   point these rows are real borrower records and this function's caller is an
   agent workflow. So the full result streams to S3 under `query-results/`
   (7-day expiry, set in infra/s3.tf) and the response carries the *shape* of
   the result -- row count, columns, types, where it landed. Rows come back
   inline only when there are few enough to be an aggregate rather than an
   extract, and even then `max_inline_rows` is a ceiling a caller can lower and
   cannot raise.

Event:
    {"sql_query": "SELECT ...", "max_rows": 200000, "max_inline_rows": 100}

Response:
    {"success": true, "row_count": 3, "columns": [...], "column_types": [...],
     "s3_uri": "s3://.../query-results/2026/08/31/<uuid>.csv", "bytes": 412,
     "truncated": false, "elapsed_ms": 118, "rows": [[...], ...] | null,
     "rows_withheld_reason": "..." | null}

Errors are returned, never raised: {"success": false, "error": "..."} so the
caller can feed the message back to SQL generation as retry feedback, which is
the same contract the local tool had.
"""
import csv
import datetime as dt
import io
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

import boto3  # noqa: E402
import db  # noqa: E402

ARTIFACTS_BUCKET = os.environ["ARTIFACTS_BUCKET"]
RESULTS_PREFIX = os.environ.get("RESULTS_PREFIX", "query-results")

# Hard ceiling, not a normal-operation limiter: set well above the 878k rows in
# the portfolio so a legitimate full-population query never trips it. A low cap
# would silently truncate an aggregate -- a wrong total portfolio EL is worse
# than a refused query.
DEFAULT_MAX_ROWS = 1_000_000
DEFAULT_MAX_INLINE_ROWS = 100
FETCH_CHUNK = 10_000
PART_SIZE = 8 * 1024 * 1024  # S3 multipart minimum is 5 MiB per part except the last

# PostgreSQL's write and privilege verbs, plus the ones that reach outside the
# database: COPY can read and write server-side files, DO runs a procedural
# block, and SET/RESET could undo the read-only session settings.
BLOCKED_KEYWORDS = re.compile(
    r"(?is)\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|"
    r"VACUUM|ANALYZE|REINDEX|CLUSTER|LOCK|MERGE|CALL|DO|SET|RESET|DISCARD|"
    r"LISTEN|NOTIFY|UNLISTEN|PREPARE|EXECUTE|DEALLOCATE|REFRESH|COMMENT|"
    r"SECURITY\s+LABEL|IMPORT\s+FOREIGN\s+SCHEMA)\b"
)

_s3 = boto3.client("s3")


def _fail(error):
    return {"success": False, "error": error}


def _validate(sql_query):
    if not sql_query or not sql_query.strip():
        return "No SQL query was supplied."

    sql = sql_query.strip().rstrip(";").strip()

    if not re.match(r"(?is)^SELECT\b", sql):
        return "Only SELECT statements are permitted."

    # pg8000 uses the extended query protocol, which rejects multiple statements
    # in one call outright -- so this is a second line rather than the only one.
    # It has to ignore semicolons inside string literals to avoid refusing a
    # legitimate query.
    if ";" in re.sub(r"'(?:[^']|'')*'", "''", sql):
        return "Only a single statement is permitted."

    if BLOCKED_KEYWORDS.search(sql):
        return "Query contains a disallowed keyword."

    return None

class _S3CsvSink:
    """
    Streams CSV to S3 in fixed-size parts, so peak memory is one part rather
    than one result set. A 878k-row extract is a few hundred MB of CSV; buffering
    that whole thing to hand to put_object is how a 512 MB Lambda dies on the
    query that matters most.
    """

    def __init__(self, bucket, key):
        self.bucket = bucket
        self.key = key
        self._reset_buffer()
        self.parts = []
        self.total_bytes = 0
        self.upload_id = _s3.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType="text/csv", ServerSideEncryption="AES256"
        )["UploadId"]

    def _reset_buffer(self):
        self.buffer = io.StringIO()
        self.writer = csv.writer(self.buffer, lineterminator="\n")

    def write_rows(self, rows):
        self.writer.writerows(rows)
        if self.buffer.tell() >= PART_SIZE:
            self._flush()

    def _flush(self):
        payload = self.buffer.getvalue().encode("utf-8")
        if not payload:
            return
        part_number = len(self.parts) + 1
        etag = _s3.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=part_number,
            Body=payload,
        )["ETag"]
        self.parts.append({"PartNumber": part_number, "ETag": etag})
        self.total_bytes += len(payload)
        self._reset_buffer()

    def close(self):
        self._flush()
        _s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts},
        )
        return self.total_bytes

    def abort(self):
        # Without this an interrupted run leaves parts in the bucket that are
        # billed for storage and are invisible to a plain ListObjects.
        try:
            _s3.abort_multipart_upload(
                Bucket=self.bucket, Key=self.key, UploadId=self.upload_id
            )
        except Exception:  # nothing useful to do about a failed cleanup
            pass

def lambda_handler(event, context):
    sql_query = (event or {}).get("sql_query") or (event or {}).get("sql")

    error = _validate(sql_query)
    if error:
        return _fail(error)

    sql_query = sql_query.strip().rstrip(";").strip()
    max_rows = min(int((event or {}).get("max_rows") or DEFAULT_MAX_ROWS), DEFAULT_MAX_ROWS)
    max_inline_rows = min(
        int((event or {}).get("max_inline_rows") or DEFAULT_MAX_INLINE_ROWS),
        DEFAULT_MAX_INLINE_ROWS,
    )

    now = dt.datetime.now(dt.timezone.utc)
    key = "%s/%s/%s.csv" % (RESULTS_PREFIX, now.strftime("%Y/%m/%d"), uuid.uuid4().hex)

    started = time.time()
    connection = None
    sink = None
    try:
        connection = db.connect()
        cursor = connection.cursor()
        cursor.execute(sql_query)

        columns = [d[0] for d in cursor.description]
        # pg8000 reports the PostgreSQL type OID; the name is what a caller can
        # act on, and pg_type is readable without extra privileges.
        type_oids = [d[1] for d in cursor.description]

        sink = _S3CsvSink(ARTIFACTS_BUCKET, key)
        sink.write_rows([columns])

        row_count = 0
        truncated = False
        inline_rows = []
        while True:
            chunk = cursor.fetchmany(FETCH_CHUNK)
            if not chunk:
                break
            if row_count + len(chunk) > max_rows:
                chunk = chunk[: max_rows - row_count]
                truncated = True
            sink.write_rows(chunk)
            if len(inline_rows) < max_inline_rows:
                inline_rows.extend(chunk[: max_inline_rows - len(inline_rows)])
            row_count += len(chunk)
            if truncated:
                break

        column_types = _type_names(cursor, type_oids)
        cursor.close()
        total_bytes = sink.close()
        sink = None

    except Exception as exc:  # bad syntax, unknown column, timeout, permission
        if sink is not None:
            sink.abort()
        # The class name matters to the caller -- a timeout is not a syntax error
        # -- and pg8000 puts the server's own message in the exception args.
        return _fail("%s: %s" % (type(exc).__name__, exc))
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    withheld = None
    if row_count > max_inline_rows:
        inline_rows = None
        withheld = (
            "%d rows is above the %d-row inline ceiling, so the rows were not returned. "
            "Read them from %s in S3, or aggregate in SQL." % (row_count, max_inline_rows, key)
        )

    return {
        "success": True,
        "row_count": row_count,
        "columns": columns,
        "column_types": column_types,
        "s3_uri": "s3://%s/%s" % (ARTIFACTS_BUCKET, key),
        "s3_key": key,
        "bytes": total_bytes,
        "truncated": truncated,
        "elapsed_ms": int((time.time() - started) * 1000),
        "rows": [list(r) for r in inline_rows] if inline_rows else None,
        "rows_withheld_reason": withheld,
    }


def _type_names(cursor, type_oids):
    """Type names for the returned columns, so a caller knows whether it got a
    numeric or a text column without inferring it from the values."""
    try:
        cursor.execute(
            "SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)",
            ([int(o) for o in type_oids],),
        )
        names = dict(cursor.fetchall())
        return [names.get(int(o), str(o)) for o in type_oids]
    except Exception:
        return [str(o) for o in type_oids]
