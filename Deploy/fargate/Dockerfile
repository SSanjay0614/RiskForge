# Python 3.9 to match the environment the models were trained in and the tools
# were written against. 3.9 is old, and choosing it is deliberate: the image
# reuses tools/feature_engineering_tool.py unchanged, so the interpreter and the
# pandas/numpy pair are part of the numerical contract, not incidental plumbing.
# Slim rather than alpine -- pandas and numpy publish manylinux wheels and no
# musl ones, so alpine would mean compiling both from source.
FROM python:3.9-slim-bookworm

# 1 -- tracebacks and log lines appear in CloudWatch as they happen instead of
# when the process exits, which matters when the process is a task that may be
# killed. 2 -- no .pyc files, since the container is never restarted.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies before source, so editing a module does not reinstall pandas.
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

# build/ is assembled by stage.py, which copies in exactly the repository files
# the two modes need and writes build/MANIFEST.json listing every one with its
# SHA-256. The build context is this directory rather than the repository root
# on purpose: with the root as context, Docker would upload Data/, Models/ and
# Database/credit_risk.db and the image contents would be whatever .dockerignore
# forgot to exclude. Here they are whatever stage.py names, which is a list you
# can read.
COPY build/ ./

# Nothing in either mode writes outside /tmp except utils/logger.py, which
# creates ./logs at import. Root is not needed for that and not wanted for
# anything else.
RUN useradd --create-home --uid 10001 riskforge \
    && mkdir -p /app/logs \
    && chown -R riskforge:riskforge /app
USER riskforge

# No CMD default mode. The mode decides whether this container computes Expected
# Loss or interest-rate risk, and a default would let a misconfigured Step
# Functions state silently run the wrong branch. Step Functions supplies the
# override; with no arguments the entrypoint exits non-zero and says so.
ENTRYPOINT ["python", "task.py"]
