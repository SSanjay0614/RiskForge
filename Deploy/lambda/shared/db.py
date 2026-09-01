"""
One PostgreSQL connection helper, shared by both VPC-attached Lambdas.

There is no password anywhere in this path. The functions authenticate with an
RDS IAM auth token, which `generate_db_auth_token` derives by SigV4-signing a
string with the role's own temporary credentials -- no API call, so a Lambda in
a private subnet with no NAT and no VPC endpoint can still get one. Three things
follow from that:

  * Nothing to rotate. `manage_master_user_password = true` makes RDS rotate the
    master password every 7 days, so a baked-in connection string would work
    through a demo and then fail a week later. This one cannot go stale; the
    token is minted per invocation and expires in 15 minutes.
  * Nothing to leak. No secret in an environment variable, in Terraform state,
    or in this public repo.
  * Read-only is enforced by the server, not by the caller. These functions
    connect as `riskforge_ro`, which holds SELECT on three tables and nothing
    else, and carries `default_transaction_read_only = on`. See
    Deploy/lambda/sql/create_readonly_role.sql -- the keyword check in the
    execute-sql handler is a fast pre-filter for better error messages, not the
    boundary.

TLS is verify-full against the bundled Amazon RDS root store: `require` would
encrypt without checking who is on the other end, which stops an eavesdropper
and not an impersonator. IAM auth in particular has to be verify-full, because
the token is a bearer credential -- handing it to an unverified endpoint hands
over database access.
"""
import os
import ssl

import boto3
import pg8000.dbapi

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ.get("DB_USER", "riskforge_ro")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Shipped in the deployment package by build.py rather than fetched at runtime:
# these functions have no route to the internet, which is the point.
CA_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rds-global-bundle.pem")

# Module-level so warm invocations reuse it. The RDS client is only ever used to
# compute a token locally, so this never touches the network.
_rds_client = None

# The statement budget, in milliseconds, and the reason it is an environment
# variable rather than the literal it used to be.
#
# 25 seconds was sized for a filtered question -- a state, a grade, a date range
# -- and it held for exactly as long as that was the only kind of question asked.
# A whole-portfolio question is a different shape: the join across all 878,317
# rows takes 6.7 seconds server-side and the extract that follows it is minutes,
# so a 25-second ceiling does not refuse a bad query, it refuses the headline
# one. Worse, it refuses it as `57014 statement timeout`, which the pipeline maps
# to QueryTooBroad -- so the largest legitimate question in the system reported
# itself as a question too vague to answer.
#
# In the environment so the ceiling is a Terraform variable rather than a
# redeploy, and so the two functions that share this module can differ: the
# extract needs minutes, the compliance check needs seconds.
DEFAULT_STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "25000"))


def _ssl_context():
    context = ssl.create_default_context(cafile=CA_BUNDLE)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def connect(statement_timeout_ms: int = None):
    """
    A fresh connection per invocation, deliberately.

    Caching the connection across invocations would save ~100 ms and cost
    correctness: the token expires, and a Lambda execution environment can be
    frozen mid-transaction for minutes, which is exactly how an idle session
    ends up holding a snapshot open on the server. Connection setup is cheap
    against queries that scan 878k rows.
    """
    if statement_timeout_ms is None:
        statement_timeout_ms = DEFAULT_STATEMENT_TIMEOUT_MS

    global _rds_client
    if _rds_client is None:
        _rds_client = boto3.client("rds", region_name=AWS_REGION)

    token = _rds_client.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=AWS_REGION
    )

    # Derived from the statement budget rather than set on its own, and this is
    # the one number in this module that had to be measured instead of reasoned
    # about. pg8000 hands `timeout` to socket.create_connection() and never
    # resets it (pg8000/core.py:206), so it is not a connect timeout -- it is
    # also the timeout on every read for the life of the connection. Any value
    # below statement_timeout is therefore a trap: a query that is slow but
    # perfectly legal dies as "InterfaceError: network error" while the server is
    # still working on it happily.
    #
    # What made it worth chasing is where that lie ends up. The message travels
    # back to SQL generation as retry feedback, the generator has nothing to fix
    # because the SQL was never wrong, it returns the identical statement, and
    # the pipeline stops at SqlUnchanged -- a cold buffer cache reported to the
    # analyst as a question the schema cannot answer.
    #
    # Measured against the deployed function at the old timeout=10:
    # `SELECT pg_sleep(5)` returned, `SELECT pg_sleep(15)` failed at 11.1s with
    # exactly that error. Streaming queries survived it by accident, because the
    # timeout is per read and rows keep arriving; the ones that died were the
    # ones that scan for more than ten seconds before emitting a first row,
    # which is why it presented as intermittent and followed a database restart.
    #
    # Staying above statement_timeout puts the decision back where it can be
    # explained: the server ends a long query, and says that it did.
    socket_timeout = statement_timeout_ms / 1000.0 + 5

    connection = pg8000.dbapi.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=token,
        ssl_context=_ssl_context(),
        timeout=socket_timeout,
        application_name="riskforge-lambda",
    )

    # Belt to the role's braces. The ALTER ROLE in create_readonly_role.sql sets
    # both of these as defaults; setting them here too means a role that was
    # rebuilt by hand without them still cannot start a write transaction or run
    # away with a Lambda's whole timeout budget.
    #
    # set_config() rather than SET: `SET` is a utility statement, and utility
    # statements take no bind parameters, so `SET statement_timeout = $1` is a
    # syntax error under pg8000's extended query protocol. set_config is an
    # ordinary function, so the value can stay a parameter instead of being
    # formatted into the statement -- which is the rule this whole module exists
    # to keep.
    cursor = connection.cursor()
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (str(int(statement_timeout_ms)),),
    )
    cursor.execute("SELECT set_config('default_transaction_read_only', 'on', false)")
    # Scaled off the statement budget rather than fixed at 30s. This one only
    # fires while a transaction is open and doing nothing, which is a window of
    # milliseconds here -- but a fixed 30s sitting underneath a 600s statement
    # budget is the same class of trap as the socket timeout above, and it would
    # present the same way: a legal query killed by a limit that was written for
    # a smaller one.
    cursor.execute(
        "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
        (str(int(statement_timeout_ms) + 30_000),),
    )
    cursor.close()
    connection.commit()

    return connection
