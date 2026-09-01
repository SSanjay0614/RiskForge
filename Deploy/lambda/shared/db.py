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


def _ssl_context():
    context = ssl.create_default_context(cafile=CA_BUNDLE)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def connect(statement_timeout_ms: int = 25_000):
    """
    A fresh connection per invocation, deliberately.

    Caching the connection across invocations would save ~100 ms and cost
    correctness: the token expires, and a Lambda execution environment can be
    frozen mid-transaction for minutes, which is exactly how an idle session
    ends up holding a snapshot open on the server. Connection setup is cheap
    against queries that scan 878k rows.
    """
    global _rds_client
    if _rds_client is None:
        _rds_client = boto3.client("rds", region_name=AWS_REGION)

    token = _rds_client.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=AWS_REGION
    )

    connection = pg8000.dbapi.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=token,
        ssl_context=_ssl_context(),
        timeout=10,
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
    cursor.execute("SELECT set_config('idle_in_transaction_session_timeout', '30000', false)")
    cursor.close()
    connection.commit()

    return connection
