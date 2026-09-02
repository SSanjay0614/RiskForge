#!/bin/sh
# One image, two runtimes.
#
# Lambda sets AWS_LAMBDA_RUNTIME_API in the execution environment and nothing else
# does, so it is the reliable signal -- more so than a build-time choice, because
# the whole point of this file is that the same image tag runs in both places. That
# is what makes the Step Functions change reversible: if in-process scoring on
# Lambda ever disagrees with the endpoints, the state machine can be pointed back
# at ecs:runTask against this identical image without a rebuild.
#
# exec rather than a child process, in both branches. Fargate stops a task with
# SIGTERM and Lambda's runtime client expects to own PID 1; a shell sitting in
# between would swallow the signal in the first case and add a process to reap in
# the second.
set -e

if [ -n "${AWS_LAMBDA_RUNTIME_API}" ]; then
    # /tmp, because Lambda mounts a container image read-only and /tmp is the only
    # writable path. utils/logger.py does `os.makedirs("logs")` and opens
    # logs/app.log at import -- a relative path, resolved against the working
    # directory -- so from /app that is an OSError: Read-only file system raised
    # during init, on every invocation, before any handler code runs. From /tmp it
    # is /tmp/logs/app.log and the same import succeeds untouched.
    #
    # Changing the working directory is safe for everything else here because
    # nothing else uses a relative path: config.py derives MODELS_DIR from
    # Path(__file__).resolve().parent, so /app/Models is found from anywhere, and
    # inputs.py stages its download through tempfile, which is already /tmp.
    # PYTHONPATH carries /app (see the Dockerfile) so awslambdaric can still
    # import task from outside it.
    cd /tmp
    exec python -m awslambdaric task.handler
fi

# No default mode here either. task.py exits 2 with its usage when --mode is
# missing, which is what a misconfigured Step Functions state should get instead of
# a valid-looking JSON computed by the wrong branch.
exec python task.py "$@"
