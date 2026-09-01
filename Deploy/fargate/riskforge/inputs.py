"""
Reads the query result set that riskforge-execute-sql streamed to S3.

This is the handoff between the data tier and the scoring tier, and the shape of
it is the reason the scoring tier holds no database credentials. execute_sql runs
the generated SELECT inside the VPC, writes CSV to s3://<artifacts>/query-results/
and returns the result's *shape* -- row count, column names, types, the S3 key.
The rows themselves never enter the Step Functions state (which has a 256 KB
limit) and never reach a caller that has not been given the object. This module
is on the other side of that boundary: an S3 key in, a DataFrame out, no
connection string anywhere in the container.

The CSV is read whole rather than in chunks, deliberately. FeatureEngineeringTool
drops rows on conditions evaluated per row, RepricingGapTool takes its reporting
date from max(issue_date) across the whole population, and ConcentrationTool
needs every segment's total to compute a share -- all three are whole-population
operations, so a chunked reader would only move the assembly somewhere less
obvious. What protects memory instead is an explicit row cap that fails loudly,
below, rather than an OOM kill that Fargate reports as exit code 137 with no
message.
"""
import os
import tempfile

import boto3
import pandas as pd

from utils.logger import logger

# 878,000 rows x 40 columns is the whole portfolio and about 300 MB in pandas;
# the engineered frame and get_dummies' intermediate copies land the peak near
# 3 GB, inside the task's 8 GB. This cap exists so that a query returning more
# than the portfolio -- a join that multiplied rows, which is a bug upstream --
# stops here with a message instead of being killed by the kernel.
MAX_ROWS = 1_200_000


def parse_uri(uri):
    """s3://bucket/key -> (bucket, key). Anything else is a caller error."""
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        raise ValueError("expected an s3:// URI, got %r" % (uri,))
    rest = uri[len("s3://"):]
    if "/" not in rest:
        raise ValueError("s3 URI has no key: %r" % (uri,))
    bucket, key = rest.split("/", 1)
    if not bucket or not key:
        raise ValueError("s3 URI is missing a bucket or a key: %r" % (uri,))
    return bucket, key


def _read_csv(handle):
    return pd.read_csv(
        handle,
        # The empty CSV field is a SQL NULL and nothing else is. pandas' default
        # NA list would also turn the strings "NA", "N/A", "null" and "None"
        # into NaN -- and emp_title is free text a borrower typed, so some of
        # them contain exactly those. A borrower whose job title is "NA" has a
        # frequency in the training map; letting the reader turn it into NaN
        # would give that row emp_title_freq 0 instead, for no reason but a
        # default.
        keep_default_na=False,
        na_values=[""],
        # The generated SELECT is `SELECT *`, so no column is guaranteed to be
        # homogeneous enough for the chunked type inference to get right. False
        # reads the column once, whole, and infers from all of it.
        low_memory=False,
    )


def load_query_result(uri, s3=None, max_rows=MAX_ROWS):
    """
    The rows of one query result, as the DataFrame the tools expect.

    Downloaded to a temporary file rather than parsed from the response stream:
    a mid-transfer failure on a 300 MB object is then a retryable download
    rather than a half-built DataFrame, and read_csv gets a seekable file, which
    is what low_memory=False needs to do a single-pass inference.
    """
    bucket, key = parse_uri(uri)
    s3 = s3 or boto3.client("s3")

    handle = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    try:
        handle.close()
        s3.download_file(bucket, key, handle.name)
        size = os.path.getsize(handle.name)
        logger.info("inputs | downloaded %s (%.1f MB)" % (uri, size / 1024 / 1024))
        df = _read_csv(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    if len(df) > max_rows:
        raise ValueError(
            "query result has %d rows, above the %d-row cap. The whole portfolio "
            "is about 878,000 rows, so a result larger than this is a join that "
            "multiplied rows rather than a broad question."
            % (len(df), max_rows)
        )

    return prepare(df)


def prepare(df):
    """
    The one transformation the Data Agent applies before either risk agent sees
    the rows: exposure_at_default as a non-destructive alias of
    outstanding_balance.

    Carried across from agents/data_agent.py, and it has to happen here for the
    same reason it happens there -- FeatureEngineeringTool validates against the
    raw schema name, while RepricingGapTool and ConcentrationTool both require
    the exposure name. Doing it once, before the branches, is what keeps the two
    branches from deriving it differently.
    """
    df = df.copy()
    if "outstanding_balance" in df.columns and "exposure_at_default" not in df.columns:
        df["exposure_at_default"] = df["outstanding_balance"]
    logger.info("inputs | %d rows, %d columns" % (len(df), len(df.columns)))
    return df
