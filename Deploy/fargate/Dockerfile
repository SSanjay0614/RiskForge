# Python 3.9 to match the environment the models were trained in and the tools
# were written against. 3.9 is old, and choosing it is deliberate: the image
# reuses tools/feature_engineering_tool.py unchanged, so the interpreter and the
# pandas/numpy pair are part of the numerical contract, not incidental plumbing.
# Slim rather than alpine -- pandas and numpy publish manylinux wheels and no
# musl ones, so alpine would mean compiling both from source.
#
# Not an AWS Lambda base image, even though this now runs as a container-image
# Lambda. public.ecr.aws/lambda/python:3.9 would change the interpreter's build
# and is a deprecated runtime besides; awslambdaric in requirements.txt provides
# the same runtime interface on top of the base image the numbers were verified
# against. entry.sh dispatches on AWS_LAMBDA_RUNTIME_API, so one image runs as
# both a Lambda function and a Fargate task.
#
# Two stages, because awslambdaric compiles a C extension and its toolchain has no
# business being in a running image. The builder installs into /deps and only the
# result is copied forward.
FROM python:3.9-slim-bookworm AS builder

WORKDIR /build

# curl's development headers and a C++ toolchain: awslambdaric builds against
# libcurl. pandas, numpy, scipy and xgboost all resolve to manylinux wheels and
# compile nothing.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        gcc g++ make cmake autoconf automake libtool libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --target /deps --requirement requirements.txt


FROM python:3.9-slim-bookworm

# 1 -- tracebacks and log lines appear in CloudWatch as they happen instead of
# when the process exits, which matters when the process is a task that may be
# killed. 2 -- no .pyc files, since the container is never restarted. 3 -- /deps
# is where the builder installed the packages, and /app is on the path explicitly
# rather than implicitly: the Lambda branch of entry.sh runs from /tmp, so the
# working directory is no longer the one holding task.py and the repository
# modules.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/deps:/app

WORKDIR /app

# libcurl itself, not its headers: awslambdaric links against it at runtime.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes libcurl4 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies before source, so editing a module does not reinstall pandas.
COPY --from=builder /deps /deps

# build/ is assembled by stage.py, which copies in exactly the repository files
# the two modes need, extracts the PD and LGD artifacts out of the model.tar.gz
# bundles the SageMaker endpoints serve, and writes build/MANIFEST.json listing
# every file with its SHA-256. The build context is this directory rather than the
# repository root on purpose: with the root as context, Docker would upload Data/,
# Models/ and Database/credit_risk.db and the image contents would be whatever
# .dockerignore forgot to exclude. Here they are whatever stage.py names, which is
# a list you can read.
COPY build/ ./
COPY entry.sh /entry.sh

# Nothing in either mode writes outside /tmp except utils/logger.py, which
# creates ./logs at import. Root is not needed for that and not wanted for
# anything else.
RUN chmod 0555 /entry.sh \
    && useradd --create-home --uid 10001 riskforge \
    && mkdir -p /app/logs \
    && chown -R riskforge:riskforge /app
USER riskforge

# No CMD default mode. The mode decides whether this container computes Expected
# Loss or interest-rate risk, and a default would let a misconfigured Step
# Functions state silently run the wrong branch. Step Functions supplies the
# override -- a command override on Fargate, a payload field on Lambda -- and with
# neither, task.py exits non-zero and says so.
ENTRYPOINT ["/entry.sh"]
