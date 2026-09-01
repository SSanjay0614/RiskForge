"""
Builds the five Lambda deployment packages.

    python Deploy/lambda/build.py

Writes dist/<function>.zip for each function in FUNCTIONS below. The two
database functions carry the handler, shared/db.py, the Amazon RDS root
certificate bundle and pg8000; the three prompt functions carry the handler and
three pure-Python modules and nothing else.

Notes on the choices here, since both are load-bearing:

  * **pg8000, not psycopg2.** psycopg2 is a C extension, so a wheel built on this
    Windows machine will not load on Lambda's Linux runtime -- packaging it means
    either psycopg2-binary's manylinux wheel pulled with --platform flags, or
    Docker, or a prebuilt layer. pg8000 is pure Python (and so are its two
    dependencies, scramp and asn1crypto), which makes the zip built here byte-for
    byte usable there. The C driver's speed advantage is per-round-trip, and these
    functions make one query per invocation.
  * **The CA bundle ships in the zip.** These functions sit in a VPC with no NAT
    and cannot fetch it at runtime -- which is the point of the network design,
    not a limitation to work around. Downloaded here at build time instead.
  * **boto3 is not vendored.** The Lambda runtime provides it. Adding a second
    copy would inflate the zip and, worse, pin a version that then diverges from
    the runtime's.
  * **The three prompt functions have no dependencies at all.** One HTTPS POST
    with urllib and one SSM call with the runtime's boto3, so their zips are a
    handful of KB and there is no wheel to be wrong about.
"""
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
SHARED = os.path.join(HERE, "shared")

CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
CA_NAME = "rds-global-bundle.pem"

# Requirements and shared files are per function, not global. Two reasons:
#
#   * The three prompt functions never open a socket to PostgreSQL, so vendoring
#     pg8000 into them would be three copies of a database driver that nothing
#     imports.
#   * shared/ is four modules now. Copying the whole directory into every zip
#     would put gemini.py in the two database functions and the 130 KB RDS
#     certificate bundle in the three that have no use for it -- and it would
#     change source_code_hash on the two functions that are already deployed and
#     passing, for no change in their code. Naming the files each function
#     actually needs keeps those two zips byte-identical, which is the whole
#     point of the deterministic build below.
#
# Pinned rather than floating: a driver version that changes underneath a
# deployment is a change nobody reviewed.
FUNCTIONS = {
    "execute_sql": {
        "requirements": ["pg8000==1.31.2"],
        "shared": ["db.py", CA_NAME],
    },
    "compliance_check": {
        "requirements": ["pg8000==1.31.2"],
        "shared": ["db.py", CA_NAME],
    },
    "guard_action": {
        "requirements": [],
        "shared": ["gemini.py", "prompts.py", "schema_postgres.py"],
    },
    "sqlgen_action": {
        "requirements": [],
        "shared": ["gemini.py", "prompts.py", "schema_postgres.py"],
    },
    "evaluator_action": {
        "requirements": [],
        "shared": ["gemini.py", "prompts.py", "schema_postgres.py"],
    },
}

def fetch_ca_bundle():
    target = os.path.join(SHARED, CA_NAME)
    if os.path.exists(target):
        print("  CA bundle already present")
        return target
    print("  downloading %s" % CA_URL)
    with urllib.request.urlopen(CA_URL, timeout=30) as response:
        data = response.read()
    if b"BEGIN CERTIFICATE" not in data:
        raise SystemExit("downloaded CA bundle does not look like PEM")
    with open(target, "wb") as f:
        f.write(data)
    print("  %.0f KB, %d certificates" % (len(data) / 1024, data.count(b"BEGIN CERTIFICATE")))
    return target


def install_dependencies(target_dir, requirements):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
         "--target", target_dir] + requirements
    )


def build(function_dir, spec):
    staging = os.path.join(BUILD, function_dir)
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    if spec["requirements"]:
        install_dependencies(staging, spec["requirements"])

    shutil.copy2(os.path.join(HERE, function_dir, "handler.py"), staging)

    shared_staging = os.path.join(staging, "shared")
    os.makedirs(shared_staging)
    for name in spec["shared"]:
        shutil.copy2(os.path.join(SHARED, name), shared_staging)

    zip_path = os.path.join(DIST, function_dir + ".zip")
    # Deterministic: files sorted and every timestamp fixed, so rebuilding
    # unchanged source produces an identical zip and Terraform sees no diff.
    # Without this every build would redeploy both functions.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, dirs, files in os.walk(staging):
            dirs.sort()
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")) or "__pycache__" in root:
                    continue
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, staging).replace(os.sep, "/")
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as f:
                    archive.writestr(info, f.read())

    return zip_path


def main():
    os.makedirs(DIST, exist_ok=True)
    print("preparing shared assets")
    fetch_ca_bundle()

    for function_dir, spec in FUNCTIONS.items():
        print("building %s" % function_dir)
        path = build(function_dir, spec)
        print("  %6.2f MB  %s" % (os.path.getsize(path) / 1024 / 1024, path))

    shutil.rmtree(BUILD, ignore_errors=True)


if __name__ == "__main__":
    main()
