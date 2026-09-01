"""
Stages the build context, builds the image, and pushes it to ECR.

    python Deploy/fargate/build_and_push.py --profile riskforge
    python Deploy/fargate/build_and_push.py --profile riskforge --no-push

Three steps, in order, each one refusing to guess:

  1. `stage.py` assembles Deploy/fargate/build/ from a named list of files and
     writes MANIFEST.json with a SHA-256 per file.
  2. The image is built from that directory and tagged twice: `latest`, which the
     task definition follows, and a content tag derived from the manifest plus
     the Dockerfile and requirements.txt.
  3. Both tags are pushed.

**The content tag is the point of this script.** `latest` answers "what runs
next"; it cannot answer "what produced this number", because it moves. The
content tag is the first 12 hex characters of a SHA-256 over every staged file's
digest and the two files that decide the environment around them -- so the same
sources give the same tag, and a changed source gives a different one. A number
in an S3 result can be traced back to an image that still exists, which is the
difference between an audit trail and a claim.

The repository itself is Terraform's (infra/ecr.tf) and this script will not
create it. A repository created here would be a resource Terraform does not know
about, and the next apply would try to create it a second time.

Docker's credentials come from an ECR authorization token piped to `docker login`
on stdin, not passed as an argument -- an argument is visible in the process list
for the duration of the command, and on a shared machine that is a credential
leak with no upside.
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
MANIFEST = os.path.join(BUILD, "MANIFEST.json")

REPO_NAME = "riskforge-risk"

# Both are part of what the image is, and neither is in the manifest: stage.py
# lists what goes *into* build/, while these two decide the interpreter, the
# pinned library versions and the user the process runs as.
TAG_INPUTS = ["Dockerfile", "requirements.txt"]


def run(command, stdin_bytes=None):
    print("  $ %s" % " ".join(command))
    process = subprocess.Popen(
        command, cwd=HERE,
        stdin=subprocess.PIPE if stdin_bytes is not None else None,
    )
    process.communicate(stdin_bytes)
    if process.returncode != 0:
        raise SystemExit("failed (exit %d): %s" % (process.returncode, command[0]))


def stage():
    print("staging the build context")
    run([sys.executable, os.path.join(HERE, "stage.py")])


def content_tag():
    """
    12 hex characters over the staged files' digests plus the Dockerfile and
    requirements.txt. Sorted, so the tag does not depend on the order stage.py
    happened to walk the list in.
    """
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)

    sha = hashlib.sha256()
    for entry in sorted(manifest["files"], key=lambda e: e["path"]):
        sha.update(("%s:%s\n" % (entry["path"], entry["sha256"])).encode("utf-8"))
    for name in TAG_INPUTS:
        with open(os.path.join(HERE, name), "rb") as f:
            sha.update(("%s:%s\n" % (name, hashlib.sha256(f.read()).hexdigest()))
                       .encode("utf-8"))

    return sha.hexdigest()[:12], len(manifest["files"])


def repository_uri(session):
    ecr = session.client("ecr")
    try:
        described = ecr.describe_repositories(repositoryNames=[REPO_NAME])
    except ecr.exceptions.RepositoryNotFoundException:
        raise SystemExit(
            "the %s repository does not exist. It is Terraform's, not this "
            "script's:\n"
            "    cd infra && terraform apply -target=aws_ecr_repository.risk "
            "-target=aws_ecr_lifecycle_policy.risk" % REPO_NAME
        )
    return described["repositories"][0]["repositoryUri"]


def login(session, registry):
    print("logging in to %s" % registry)
    token = session.client("ecr").get_authorization_token()
    encoded = token["authorizationData"][0]["authorizationToken"]
    user, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    run(["docker", "login", "--username", user, "--password-stdin", registry],
        stdin_bytes=password.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--skip-stage", action="store_true",
                        help="reuse build/ as it stands, for iterating on the Dockerfile")
    parser.add_argument("--no-push", action="store_true",
                        help="build and tag only, no registry call")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    # docker writes straight to the terminal while this script's own prints go
    # through a block buffer when the output is piped, which interleaves them in
    # an order that did not happen. Line buffering keeps the log readable.
    sys.stdout.reconfigure(line_buffering=True)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    if not args.skip_stage:
        stage()
    if not os.path.exists(MANIFEST):
        raise SystemExit("%s is missing -- run without --skip-stage" % MANIFEST)

    tag, file_count = content_tag()
    uri = repository_uri(session)
    registry = uri.split("/", 1)[0]

    print("building %s:%s from %d staged files" % (REPO_NAME, tag, file_count))
    command = ["docker", "build"]
    if args.no_cache:
        command.append("--no-cache")
    command += [
        # Explicit rather than implied. This machine is amd64 and Fargate is
        # configured X86_64, so the default would be right today -- but a
        # Dockerfile that only builds correctly on the machine it was written on
        # is a trap for whoever runs it next, and an arm64 build would fail on
        # the task with exec format error, which reads like a broken image.
        "--platform", "linux/amd64",
        "--tag", "%s:%s" % (uri, tag),
        "--tag", "%s:latest" % uri,
        ".",
    ]
    run(command)

    if args.no_push:
        print("\nbuilt %s:%s and :latest, not pushed" % (uri, tag))
        return 0

    login(session, registry)
    for reference in ("%s:%s" % (uri, tag), "%s:latest" % uri):
        print("pushing %s" % reference)
        run(["docker", "push", reference])

    images = session.client("ecr").describe_images(
        repositoryName=REPO_NAME, imageIds=[{"imageTag": tag}])["imageDetails"][0]
    print()
    print("pushed    %s:%s" % (uri, tag))
    print("digest    %s" % images["imageDigest"])
    print("size      %.0f MB compressed" % (images["imageSizeInBytes"] / 1024 / 1024))
    print("latest    also points here")
    print()
    print("The task definition follows :latest, so the next run picks this up "
          "with no Terraform apply. To pin it instead, set risk_image_tag = "
          "\"%s\" in infra/terraform.tfvars." % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
