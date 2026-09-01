#!/usr/bin/env bash
#
# Creates the riskforge_ro database role. Runs ON the EC2 app host, not on a
# laptop -- the database has no public address and its security group admits
# only this host and the Lambdas.
#
# Same connection pattern as Database/migration/run_migration.sh: the master
# password is read from Secrets Manager into this process's environment and is
# never written to disk, never echoed, and never passed as an argv element
# (which would put it in every `ps` listing on the box).
#
#   bash run_readonly_role.sh
#
# Idempotent. Re-running it is safe and re-prints the verification block.
set -euo pipefail

AWS_REGION=${AWS_REGION:-us-east-1}
S3_BUCKET=${S3_BUCKET:-riskforge-artifacts-132467638791}
DB_HOST=${DB_HOST:-riskforge-db.cuf0ayeqgydh.us-east-1.rds.amazonaws.com}
DB_NAME=${DB_NAME:-riskforge}

WORK_DIR=${WORK_DIR:-/var/tmp/riskforge-ro}
CA_BUNDLE="${WORK_DIR}/rds-global-bundle.pem"

step() { printf '\n=== %s ===\n' "$1"; }

if ! command -v psql >/dev/null 2>&1; then
  step "installing postgresql client"
  sudo dnf install -y postgresql16 >/dev/null
fi
psql --version

step "preparing ${WORK_DIR}"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

step "fetching the DDL and the RDS root certificate bundle"
aws s3 cp "s3://${S3_BUCKET}/sql/create_readonly_role.sql" . \
  --region "$AWS_REGION" --only-show-errors
curl -fsS -o "$CA_BUNDLE" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ls -l create_readonly_role.sql "$CA_BUNDLE"

# Passed in, not discovered. Looking the ARN up with rds:DescribeDBInstances
# would be tidier, but this host's role deliberately holds no RDS API
# permissions at all -- iam_ec2.tf grants it S3 on one bucket and
# GetSecretValue on one secret ARN, and widening that so a one-off setup script
# can save a paste is the wrong trade. `terraform output db_secret_arn` is where
# the value below comes from; override it if the instance is ever rebuilt, since
# RDS mints a fresh secret with a new uuid each time.
DB_SECRET=${DB_SECRET:-arn:aws:secretsmanager:us-east-1:132467638791:secret:rds!db-65072243-bd73-4291-a24e-f912825c996b-B6hJ4f}

step "reading master credentials from Secrets Manager"
echo "secret: ${DB_SECRET}"

SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "$DB_SECRET" --region "$AWS_REGION" \
  --query SecretString --output text)
PGUSER=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')
PGPASSWORD=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
unset SECRET_JSON
export PGUSER PGPASSWORD
export PGHOST="$DB_HOST" PGPORT=5432 PGDATABASE="$DB_NAME"

# verify-full, not require: `require` encrypts but does not check who is on the
# other end. This validates the chain and the hostname as well.
export PGSSLMODE=verify-full PGSSLROOTCERT="$CA_BUNDLE"
echo "connecting as ${PGUSER} to ${PGHOST}/${PGDATABASE} with sslmode=${PGSSLMODE}"

step "create_readonly_role.sql"
psql -v ON_ERROR_STOP=1 -f create_readonly_role.sql

# The grants are only half of it -- the Lambdas authenticate as this role, and
# rds_iam membership is what makes the token acceptable. Checked here so a
# missing GRANT surfaces now rather than as a 28P01 from a Lambda.
step "confirming rds_iam membership"
psql -v ON_ERROR_STOP=1 -t -c \
  "SELECT 'riskforge_ro in rds_iam: ' || pg_has_role('riskforge_ro','rds_iam','member')::text;"

step "done -- now run, from the laptop: python Deploy/lambda/test_functions.py --profile riskforge"
