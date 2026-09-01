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

3. **How the rows get out, and why not one at a time.** The obvious shape --
   `cursor.fetchmany()` in a loop, `csv.writer` on the way past -- was what this
   handler did first, and it was measurably and structurally wrong for the
   largest question in the system.

   Measured: 80,000 rows took 24.8 seconds, 17 MB at 0.69 MB/s, which puts the
   whole 878,317-row book at roughly four minutes. Against that, the *server*
   scans and joins all 878,317 rows in 6.7 seconds
   (`SELECT COUNT(*) FROM loans JOIN borrowers USING (loan_id)`). So ~97% of the
   time was never the database: it was pg8000 -- pure Python, by choice, because
   psycopg2 is a C extension that cannot be packaged from a Windows machine --
   decoding 39 fields per row into Python objects, and csv.writer encoding them
   straight back to text.

   Structural, and the worse half: pg8000 accumulates the entire result set
   before `fetchmany` returns a single row (`pg8000/core.py:824`,
   `context.rows.append(row)` in `handle_DATA_ROW`). The streaming sink below is
   real, but it only ever bounded the *write* side; the read side still
   materialised 878k rows of Python objects first, which is one to three GB and
   an out-of-memory kill on the one query the demo exists to answer.

   So the rows are not read at all. `COPY (<query>) TO STDOUT WITH (FORMAT csv)`
   makes PostgreSQL do the CSV encoding itself, in C, and pg8000 hands the raw
   bytes to a stream (`pg8000/core.py:460`) without ever building a row. Peak
   memory becomes one 8 MB part, for real this time, and the per-field Python
   work disappears rather than getting a bigger CPU thrown at it.

   `COPY ... TO STDOUT` needs no privilege beyond the SELECT `riskforge_ro`
   already holds -- it is `COPY ... TO '/path'` that requires superuser or
   `pg_write_server_files`, and that distinction is exactly why this is safe to
   use here. It is also why COPY stays in BLOCKED_KEYWORDS: the caller may never
   send one, and this handler builds its own around SQL that has already passed
   the whole of `_validate`.

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
PART_SIZE = 8 * 1024 * 1024  # S3 multipart minimum is 5 MiB per part except the last

# Enough of the front of the CSV to recover a small result inline without asking
# the database a second time. 512 KB holds far more than DEFAULT_MAX_INLINE_ROWS
# rows of a 39-column extract, and it is kept whatever the result size so the
# decision to return rows or withhold them stays a pure function of row_count.
HEAD_TEE_BYTES = 512 * 1024

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

class _S3ByteSink:
    """
    Forwards the bytes PostgreSQL emits straight into an S3 multipart upload, in
    fixed-size parts, and keeps the first HEAD_TEE_BYTES of them so a small
    result can still be returned inline without a second query.

    A byte sink rather than the csv.writer one it replaces, because nothing on
    this path builds a Python row any more -- see point 3 of the module
    docstring. `write` receives raw bytes, which is what pg8000 hands a stream
    that is not a TextIOBase (pg8000/core.py:460), so this class must not inherit
    from one.
    """

    def __init__(self, bucket, key):
        self.bucket = bucket
        self.key = key
        self.buffer = bytearray()
        self.head = bytearray()
        self.head_truncated = False
        self.parts = []
        self.total_bytes = 0
        self.upload_id = _s3.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType="text/csv", ServerSideEncryption="AES256"
        )["UploadId"]

    def write(self, data):
        self.buffer += data
        room = HEAD_TEE_BYTES - len(self.head)
        if room > 0:
            self.head += data[:room]
        elif not self.head_truncated:
            # Recorded rather than inferred, because the last line in a full head
            # is almost certainly cut mid-row and must not be parsed.
            self.head_truncated = True
        if len(self.buffer) >= PART_SIZE:
            self._flush()

    def _flush(self):
        if not self.buffer:
            return
        part_number = len(self.parts) + 1
        payload = bytes(self.buffer)
        etag = _s3.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=part_number,
            Body=payload,
        )["ETag"]
        self.parts.append({"PartNumber": part_number, "ETag": etag})
        self.total_bytes += len(payload)
        self.buffer = bytearray()

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

def _copy_statement(sql_query, max_rows):
    """
    Wraps a validated SELECT in a COPY that PostgreSQL encodes to CSV itself.

    The interpolation here is deliberate and is safe for one reason only: by the
    time this is called, `sql_query` has passed every check in `_validate` -- it
    begins with SELECT, contains no semicolon outside a string literal, and
    contains none of BLOCKED_KEYWORDS. A caller cannot append a second statement
    and cannot smuggle a COPY of its own. Nothing here may be reached on an
    unvalidated string.

    The `LIMIT` is the row ceiling, applied by the server so the rows above it
    are never encoded rather than encoded and then thrown away. It nests: an
    inner query that already carries its own LIMIT stays correct.
    """
    return (
        "COPY (SELECT * FROM (%s) AS q LIMIT %d) TO STDOUT "
        "WITH (FORMAT csv, HEADER true)" % (sql_query, int(max_rows))
    )


def _describe(cursor, sql_query):
    """
    Column names and type names, without fetching a row.

    COPY returns bytes and a count, not a cursor description, so the shape of the
    result has to be asked for separately. `LIMIT 0` plans the query and returns
    the RowDescription without executing it, which costs nothing measurable.
    """
    cursor.execute("SELECT * FROM (%s) AS q LIMIT 0" % sql_query)
    cursor.fetchall()
    # Read both off the description before anything else runs on this cursor:
    # the next execute() replaces it.
    columns = [d[0] for d in cursor.description]
    type_oids = [d[1] for d in cursor.description]
    return columns, _type_names(cursor, type_oids)


def _head_rows(sink, want):
    """The first `want` data rows, parsed back out of the tee'd head of the CSV.

    csv.reader rather than a split on newlines, because a quoted field may
    legally contain one and splitting would produce rows that were never in the
    result."""
    rows = list(csv.reader(io.StringIO(bytes(sink.head).decode("utf-8", "replace"))))
    if sink.head_truncated and rows:
        rows = rows[:-1]  # last line was cut mid-row
    return [list(r) for r in rows[1 : want + 1]]  # rows[0] is the CSV header


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

        columns, column_types = _describe(cursor, sql_query)

        sink = _S3ByteSink(ARTIFACTS_BUCKET, key)
        cursor.execute(_copy_statement(sql_query, max_rows), stream=sink)
        # PostgreSQL's `COPY <n>` completion tag, which pg8000 parses into
        # rowcount (pg8000/core.py:796). Counts data rows, not the header.
        row_count = cursor.rowcount

        inline_rows = _head_rows(sink, max_inline_rows) if row_count <= max_inline_rows else None
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

    # `>=` rather than `>`: the ceiling is applied as a server-side LIMIT, so a
    # result that lands exactly on it cannot be told apart from one cut short by
    # it. Over-reporting truncation is the safe direction of that ambiguity.
    truncated = row_count >= max_rows

    withheld = None
    if row_count > max_inline_rows:
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
        "rows": inline_rows or None,
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
