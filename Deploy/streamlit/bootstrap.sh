#!/bin/bash
# Puts the interface on the EC2 host and leaves it running under systemd.
#
# Run it through Session Manager, which is the only way onto this instance --
# there is no SSH key and no port 22 rule:
#
#     aws ssm start-session --profile riskforge --target i-04f8e67657b01dc25
#     sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/SSanjay0614/RiskForge/main/Deploy/streamlit/bootstrap.sh)"
#
# or, if the repository is already checked out, `sudo bash bootstrap.sh`.
#
# Idempotent: run it again after a push and it fetches, reinstalls and restarts.
# Nothing here holds a credential. The instance role supplies them, and the only
# ones it has are permission to start one state machine and describe one of its
# executions -- see infra/iam_ec2.tf.
set -euo pipefail

REPO_URL="${RISKFORGE_REPO:-https://github.com/SSanjay0614/RiskForge.git}"
BRANCH="${RISKFORGE_BRANCH:-main}"
ROOT="/opt/riskforge"
CHECKOUT="$ROOT/RiskForge"
VENV="$ROOT/venv"
SERVICE="riskforge"
RUN_AS="ec2-user"

echo "== packages"
dnf install -y python3 python3-pip git >/dev/null

echo "== $CHECKOUT"
install -d -o "$RUN_AS" -g "$RUN_AS" "$ROOT"
if [ -d "$CHECKOUT/.git" ]; then
  runuser -u "$RUN_AS" -- git -C "$CHECKOUT" fetch --depth 1 origin "$BRANCH"
  runuser -u "$RUN_AS" -- git -C "$CHECKOUT" reset --hard "origin/$BRANCH"
else
  # Shallow, single branch: the history is not needed here and the clone is
  # smaller for it. Models/ and Data/ are gitignored, so this pulls no artifact
  # and no portfolio -- which is the point, since this host reads neither.
  runuser -u "$RUN_AS" -- git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CHECKOUT"
fi

echo "== $VENV"
if [ ! -x "$VENV/bin/python" ]; then
  runuser -u "$RUN_AS" -- python3 -m venv "$VENV"
fi
runuser -u "$RUN_AS" -- "$VENV/bin/pip" install --quiet --upgrade pip
runuser -u "$RUN_AS" -- "$VENV/bin/pip" install --quiet \
  -r "$CHECKOUT/Deploy/streamlit/requirements.txt"

# HOME and MPLCONFIGDIR in the unit point here; create them now so the first
# start is not also the first mkdir.
install -d -o "$RUN_AS" -g "$RUN_AS" "$ROOT/.streamlit" "$ROOT/.matplotlib"

echo "== systemd"
install -m 0644 "$CHECKOUT/Deploy/streamlit/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 3
systemctl --no-pager --lines=0 status "$SERVICE" || true
echo
echo "== reachable at http://$(curl -fsS -H "X-aws-ec2-metadata-token: $(
  curl -fsS -X PUT http://169.254.169.254/latest/api/token \
       -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" \
  http://169.254.169.254/latest/meta-data/public-ipv4):8501"
echo "   from var.my_ip_cidr only. Logs: journalctl -u $SERVICE -f"
