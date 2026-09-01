#!/usr/bin/env bash
#
# Phase 6 driver -- runs ON the EC2 app host, not on a laptop.
#
# The database has no public address and its security group admits only this
# host's SG and the Lambda SG, so there is no route to it from anywhere else.
# That is the point of the network design, and it means the migration runs here.
#
# Invoked through SSM Send Command rather than SSH: there is no port 22 open and
# no key pair to leak into a public repo.
#
# Required environment:
#   S3_BUCKET    artifacts bucket holding migration/
#   DB_HOST      RDS endpoint hostname, without the :5432
#   DB_NAME      riskforge
#   DB_SECRET    Secrets Manager ARN of the RDS-managed master credentials
#   AWS_REGION   us-east-1
#
# Optional:
#   KEEP_LOCAL=1   leave the SQLite file and CSVs on the instance afterwards
#
set -euo pipefail

: "${S3_BUCKET:?}" "${DB_HOST:?}" "${DB_NAME:?}" "${DB_SECRET:?}" "${AWS_REGION:?}"

WORK_DIR=${WORK_DIR:-/var/tmp/riskforge-migration}
PREFIX="s3://${S3_BUCKET}/migration"
CA_BUNDLE="${WORK_DIR}/rds-global-bundle.pem"

step() { printf '\n=== %s ===\n' "$1"; }

step "preparing ${WORK_DIR}"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

if ! command -v psql >/dev/null 2>&1; then
  step "installing postgresql client"
  sudo dnf install -y postgresql16 >/dev/null
fi
psql --version

step "fetching migration artifacts from ${PREFIX}"
# Skip the 100 MiB snapshot if a previous run already pulled it -- reruns of this
# script should be cheap, and the load steps below are the part worth repeating.
if [[ ! -f credit_risk.db && ! -f credit_risk.db.gz ]]; then
  aws s3 cp "${PREFIX}/credit_risk.db.gz" . --region "$AWS_REGION" --only-show-errors
else
  echo "snapshot already present, skipping download"
fi
aws s3 cp "${PREFIX}/" . --recursive --region "$AWS_REGION" \
  --exclude '*' --include '*.sql' --include 'export_csv.py' --only-show-errors
ls -la

if [[ ! -f credit_risk.db ]]; then
  step "decompressing the SQLite snapshot"
  gunzip -k credit_risk.db.gz
fi
ls -l credit_risk.db

# verify-full, not require: `require` encrypts but does not check who is on the
# other end, so it stops an eavesdropper and not an impersonator. With the RDS
# root bundle and the real endpoint hostname, this validates the certificate
# chain and the hostname too.
step "fetching the Amazon RDS root certificate bundle"
curl -fsS -o "$CA_BUNDLE" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
openssl storeutl -noout -certs "$CA_BUNDLE" 2>/dev/null | tail -1 || true

step "exporting SQLite to CSV"
python3 export_csv.py credit_risk.db "$WORK_DIR"

# The password is read from Secrets Manager into this process's environment and
# never written to disk, never echoed, and never passed as an argv element (which
# would put it in every ps listing on the box).
step "reading master credentials from Secrets Manager"
SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "$DB_SECRET" --region "$AWS_REGION" \
  --query SecretString --output text)
PGUSER=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')
PGPASSWORD=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
unset SECRET_JSON
export PGUSER PGPASSWORD
export PGHOST="$DB_HOST" PGPORT=5432 PGDATABASE="$DB_NAME"
export PGSSLMODE=verify-full PGSSLROOTCERT="$CA_BUNDLE"
echo "connecting as ${PGUSER} to ${PGHOST}/${PGDATABASE} with sslmode=${PGSSLMODE}"

# Proof that rds.force_ssl is doing its job, rather than an assumption that it
# is. The server must refuse this, so a success here is the failure case.
step "checking that the server rejects unencrypted connections"
if PGSSLMODE=disable psql -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1; then
  echo "FAIL: the server accepted a non-TLS connection -- rds.force_ssl is not in effect"
  exit 1
fi
echo "OK: plaintext connection refused"

step "connection and TLS details"
psql -v ON_ERROR_STOP=1 -c "SELECT version();" \
  -c "SELECT ssl, version AS tls, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();"

# Reported only if the engine exposes it as a queryable GUC, which it does not
# always do even when enforcement is active -- so this is informational and must
# not fail the run. The check above is the one that matters: the server refused a
# plaintext connection, which is the behaviour, not the setting's label.
psql -c "SHOW rds.force_ssl;" 2>/dev/null \
  || echo "(rds.force_ssl is not queryable on this engine build; enforcement was proven above)"

step "01_schema.sql -- creating tables"
psql -v ON_ERROR_STOP=1 -q -f 01_schema.sql

# loans first: borrowers.loan_id has a foreign key into it, so the reverse order
# would fail on the very first row.
step "loading loans"
time psql -v ON_ERROR_STOP=1 -q -f load_loans.sql
step "loading borrowers"
time psql -v ON_ERROR_STOP=1 -q -f load_borrowers.sql
step "loading risk_limits"
psql -v ON_ERROR_STOP=1 -q -f load_risk_limits.sql

step "02_finalize.sql -- indexes, identity sequence, statistics"
time psql -v ON_ERROR_STOP=1 -q -f 02_finalize.sql

step "03_verify.sql -- fingerprint (compare against the expected block in the file)"
psql -v ON_ERROR_STOP=1 -f 03_verify.sql

step "on-disk size"
psql -v ON_ERROR_STOP=1 -c "
  SELECT relname AS table, pg_size_pretty(pg_total_relation_size(c.oid)) AS total
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY pg_total_relation_size(c.oid) DESC;"

if [[ "${KEEP_LOCAL:-0}" != "1" ]]; then
  # 878,317 borrower records do not need to sit on the application host after the
  # load. The copy in S3 (private, encrypted, versioned by the bucket policy) is
  # the one that is meant to persist.
  step "removing local copies of the borrower data"
  rm -f "$WORK_DIR"/*.csv credit_risk.db credit_risk.db.gz
  ls -la "$WORK_DIR"
fi

step "done"
