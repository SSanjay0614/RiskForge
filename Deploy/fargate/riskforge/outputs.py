"""
Writes a branch's aggregates to S3, and refuses to write anything else.

Step Functions cannot read a Fargate task's stdout -- a task returns an exit code
and nothing more -- so the result has to land somewhere the next state can fetch
it. S3, under the same private bucket the query CSV came from, with a key the
task was told to use.

**The refusal is the point of this module.** Both branches hold the whole
retrieved population in memory: the score branch has per-loan PD, LGD and
Expected Loss, the rates branch has raw borrower rows including emp_title. What
is supposed to leave is sums, shares and weighted averages. "Supposed to" is a
property of the code that builds the payload, and that code will be edited by
somebody adding a field to a dashboard -- so it is checked here instead, at the
one place every byte passes through:

  * A key anywhere in the payload whose name belongs to per-loan data fails the
    write. Same approach as riskforge-evaluator-action's event scan, and the same
    reasoning: naming the shapes that must not appear is more durable than
    reviewing each new field.
  * A list or dict larger than a portfolio has segments fails the write. Every
    legitimate collection here is small and bounded -- four repricing buckets,
    four rate shocks, fourteen purposes, fifty-one states, four risk tiers. An
    878,000-element array is a per-loan column that got renamed to something the
    name check did not catch.
  * A payload over the size ceiling fails the write, for the same reason from the
    other direction: aggregates do not get large, so a large payload is rows.

None of these fires in normal operation. They exist because the alternative is a
promise in a docstring.
"""
import datetime as dt
import json

import boto3

from utils.logger import logger

# Per-loan column names, and the names a frame gets when somebody serialises one.
BANNED_KEYS = {
    "rows", "row", "records", "record", "data", "sample", "samples",
    "raw", "preview", "head", "values",
    "scored_df", "engineered_df", "rows_df", "frame", "dataframe",
    "loan_id", "loan_ids", "emp_title", "emp_titles", "annual_inc",
    "predicted_pd", "predicted_lgd", "expected_loss_per_loan", "risk_tiers",
}

# Fifty-one states is the largest legitimate collection. 250 leaves room for a
# segmentation nobody has added yet and is three orders of magnitude below a
# per-loan array.
MAX_COLLECTION = 250

# 256 KB is also the Step Functions state size limit, so a payload that fits here
# is one a later state could carry inline if it ever needed to.
MAX_BYTES = 256 * 1024


def _offending(node, path="result"):
    """The path of the first thing that must not be written, or None."""
    if isinstance(node, dict):
        if len(node) > MAX_COLLECTION:
            return "%s has %d keys, above the %d-key ceiling" % (path, len(node), MAX_COLLECTION)
        for key, value in node.items():
            if str(key).lower() in BANNED_KEYS:
                return "%s.%s is a per-loan field name" % (path, key)
            found = _offending(value, "%s.%s" % (path, key))
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        if len(node) > MAX_COLLECTION:
            return "%s has %d entries, above the %d-entry ceiling" % (
                path, len(node), MAX_COLLECTION)
        for index, value in enumerate(node):
            found = _offending(value, "%s[%d]" % (path, index))
            if found:
                return found
    return None


def check(payload):
    """Raises if `payload` carries anything per-loan. Returns the encoded bytes."""
    offending = _offending(payload)
    if offending:
        raise ValueError(
            "refusing to write the result: %s. This task holds the whole retrieved "
            "population and only aggregates are allowed out of it." % offending
        )

    # allow_nan=False for a second reason than in scoring.py: a NaN written here
    # becomes the bare token NaN in the object, which is not JSON, and every
    # consumer downstream -- Step Functions, a Lambda, the Streamlit app --
    # fails on it at a distance from the cause.
    body = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True).encode("utf-8")

    if len(body) > MAX_BYTES:
        raise ValueError(
            "refusing to write the result: %d bytes, above the %d-byte ceiling. "
            "Aggregates do not get this large." % (len(body), MAX_BYTES)
        )
    return body


def write(payload, bucket, key, s3=None):
    """Checked, encoded, and put. Returns the s3:// URI."""
    body = check(payload)
    s3 = s3 or boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        # The bucket default is AES256 already; stated on the request so the
        # object is encrypted even if the bucket policy is ever loosened.
        ServerSideEncryption="AES256",
    )
    uri = "s3://%s/%s" % (bucket, key)
    logger.info("outputs | wrote %d bytes to %s" % (len(body), uri))
    return uri


def envelope(mode, source_uri, payload, started_at, row_count):
    """
    The result, wrapped in the provenance a reader needs to know what it is
    looking at: which branch produced it, which query result it was computed
    from, how many rows that was, and how long it took.

    The source URI is here rather than only in the task's logs because the number
    and the population it describes are the same fact -- an Expected Loss rate
    without the query that produced it is not auditable.
    """
    finished = dt.datetime.now(dt.timezone.utc)
    return {
        "mode": mode,
        "success": True,
        "source_uri": source_uri,
        "row_count": row_count,
        "started_at": started_at.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms": int((finished - started_at).total_seconds() * 1000),
        "result": payload,
    }
