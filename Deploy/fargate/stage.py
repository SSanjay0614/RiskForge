"""
Assembles the Docker build context for the risk container.

    python Deploy/fargate/stage.py

Copies into Deploy/fargate/build/ exactly the repository files the two modes
need, then writes build/MANIFEST.json listing every file with its SHA-256 and
its source path. Run this before `docker build`; build_and_push.py runs it for
you.

**The container reuses the repository's risk code rather than reimplementing
it.** tools/feature_engineering_tool.py, tools/repricing_gap_tool.py and
tools/concentration_tool.py are copied in byte-for-byte and imported as they
are. Only the part that cannot be reused is rewritten: ExpectedLossTool loads
the PD and LGD models with joblib.load, and the point of Phase 8 was that those
two models are SageMaker endpoints now -- so riskforge/scoring.py replaces that
one class and nothing else.

The alternative was to port all three tools into this directory. That would have
produced a second implementation of a credit risk calculation, and a second
implementation is a thing that starts identical and then diverges -- someone
fixes a bucket boundary in tools/ and the deployed numbers quietly stop matching
the numbers the local app shows. Copying makes "the container computes what the
application computes" true by construction rather than true as of the last time
somebody checked. test_task.py checks it anyway.

Two consequences of that choice, both deliberate:

  * **config.py comes along unchanged.** The tools do `from config import
    ADDR_STATE_FREQ_MAP_PATH`, config.py derives its paths from its own location,
    and it lands at /app/config.py -- so /app/Models is where it looks and where
    the two maps are put. No path rewriting, no environment variable, nothing to
    keep in sync.
  * **The two frequency maps ship as the joblib files training wrote**, including
    emp_title_freq_map.joblib, which is 378,168 borrower-written job titles.
    Hashing the keys was considered and rejected: it would mean hashing the
    column before FeatureEngineeringTool.run() sees it, and a hash that disagreed
    with the one used to build the map produces NaN for every row, which the tool
    fills with 0 -- every borrower silently assigned the same employment
    frequency, no error raised, Expected Loss wrong. That is the exact train/serve
    skew the tool's own docstring warns about, and it is a worse risk than a job
    titles dictionary sitting in a private ECR repository. riskforge/features.py
    asserts the match rate instead, which catches the failure the hash would have
    introduced and also the ones it would not have.

What is NOT copied is as much of the point as what is. No Data/, no
Database/credit_risk.db, no PD or LGD model file, no .env, no llm/, no agents/,
no memory/. The container reads one CSV from S3, calls two endpoints and writes
JSON back; it holds no database credentials and has no route to the database.
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.join(HERE, "build")

# The two model.tar.gz bundles build_artifacts.py wrote and Terraform points the
# SageMaker endpoints at. Staged into the image so that bulk scoring can run in
# this process instead of over 880 HTTP requests -- see riskforge/scoring_local.py.
SAGEMAKER_DIST = os.path.join(REPO, "Deploy", "sagemaker", "dist")

# What comes out of each bundle, and where it lands. config.py derives MODELS_DIR
# from its own location, so Models/ in the build context is /app/Models in the
# image -- the directory the frequency maps already use.
ARTIFACT_BUNDLES = {
    "pd": ("pd-model", "Models/pd-endpoint"),
    "lgd": ("lgd-model", "Models/lgd-endpoint"),
}

# manifest.json names the rest, so it is the one file that must be present.
# calibration.json exists for PD only -- LGD's manifest sets calibration_file to
# null -- so it is extracted when present rather than required.
ARTIFACT_MEMBERS = ("manifest.json", "booster.json", "calibration.json")

# Repository files, copied verbatim. Named one by one rather than by directory:
# tools/ holds twelve modules and dmodels/ holds twelve more, and the ones absent
# from this list are absent from the image. expected_loss_tool.py is the notable
# absence: it is the only one of the four risk tools that loads the PD and LGD
# models with joblib, and those two models are SageMaker endpoints now, so
# riskforge/scoring.py stands in its place.
REPO_FILES = [
    # config.py's own location decides MODELS_DIR, so it must land at /app.
    "config.py",
    "utils/logger.py",
    "tools/base_tool.py",
    "tools/feature_engineering_tool.py",
    "tools/concentration_tool.py",
    "tools/repricing_gap_tool.py",
    # Basel III IRB risk-weighted assets. It is here rather than in
    # riskforge-compliance-check because it needs per-loan PD, LGD and EAD for
    # the whole portfolio -- see that function's docstring, which says so and
    # returns a null regulatory_capital with the reason attached.
    "tools/regulatory_capital_tool.py",
    "dmodels/feature_engineering_result.py",
    "dmodels/concentration_result.py",
    "dmodels/repricing_gap_result.py",
    "dmodels/regulatory_capital_result.py",
    # Not for a tool that runs here -- riskforge/scoring.py returns this type,
    # because returning the same shape ExpectedLossTool returns is what lets
    # test_task.py compare the two directly.
    "dmodels/expected_loss_result.py",
    # Frequency encodings from the training run. Recomputing them from a runtime
    # subset would be train/serve skew; see FeatureEngineeringTool's docstring.
    "Models/addr_state_freq_map.joblib",
    "Models/emp_title_freq_map.joblib",
    # Feature NAME lists, not models. Two reasons to carry them: each
    # endpoint is then sent exactly the columns its model uses, which halves
    # the LGD payload, and a feature the engineering step failed to produce
    # is caught before the network call rather than reported back in
    # filled_features afterwards.
    #
    # pd_risk_tier_cutoffs.joblib is deliberately NOT here. The PD endpoint
    # assigns tiers from cutoffs travelling inside its own model artifact, so
    # a copy in the container would be a second place a tier boundary is
    # defined -- and the two would not be updated together.
    "Models/pd_model_feature_names.joblib",
    "Models/lgd_model_feature_names.joblib",
]

# This directory's own files.
LOCAL_FILES = [
    "task.py",
    "riskforge/__init__.py",
    "riskforge/inputs.py",
    "riskforge/features.py",
    "riskforge/scoring.py",
    "riskforge/scoring_local.py",
    "riskforge/credit.py",
    "riskforge/rates.py",
    "riskforge/outputs.py",
]

# tools/, dmodels/ and utils/ have no __init__.py in the repository -- they are
# namespace packages, and adding one here would be a difference between the
# imported module and the repository's. Only riskforge/ has one, because it is
# this directory's own package.
def digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def stage_one(source_root, relative, entries):
    source = os.path.join(source_root, relative)
    if not os.path.exists(source):
        raise SystemExit("missing: %s" % source)
    target = os.path.join(BUILD, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)
    entries.append({
        "path": relative.replace(os.sep, "/"),
        "sha256": digest(target),
        "bytes": os.path.getsize(target),
        "source": os.path.relpath(source, REPO).replace(os.sep, "/"),
    })


def stage_endpoint_artifacts(entries):
    """
    The PD and LGD artifacts, taken out of the tarballs SageMaker serves, plus the
    handler that serves them.

    Extracting from model.tar.gz rather than re-deriving from the .joblib models
    is the whole point. build_artifacts.py already proved those bundles score
    identically to the notebook models, at zero tolerance, and Terraform points
    the endpoints at these exact bytes -- so a container that reads the same
    booster JSON, the same 227 isotonic knots and the same manifest is running the
    deployed model rather than a second copy of it that started out equal.

    The handler is asserted, not assumed. code/inference.py inside each bundle is
    compared with Deploy/sagemaker/inference.py byte-for-byte, so "the container
    runs the deployed inference code" fails the build if it stops being true --
    which is exactly the failure that would otherwise show up as two numbers that
    quietly disagree.
    """
    handler_source = os.path.join(REPO, "Deploy", "sagemaker", "inference.py")
    if not os.path.exists(handler_source):
        raise SystemExit("missing: %s" % handler_source)
    with open(handler_source, "rb") as f:
        handler_bytes = f.read()

    for kind, (bundle, target_relative) in sorted(ARTIFACT_BUNDLES.items()):
        tar_path = os.path.join(SAGEMAKER_DIST, bundle, "model.tar.gz")
        if not os.path.exists(tar_path):
            raise SystemExit(
                "missing: %s\nRun `python Deploy/sagemaker/build_artifacts.py` "
                "first -- the image scores with the endpoint's own artifacts, so "
                "they have to exist before it can be built." % tar_path
            )

        target = os.path.join(BUILD, target_relative)
        os.makedirs(target, exist_ok=True)

        with tarfile.open(tar_path, "r:gz") as tar:
            names = tar.getnames()

            packaged = tar.extractfile("code/inference.py")
            if packaged is None:
                raise SystemExit("%s has no code/inference.py" % tar_path)
            if packaged.read() != handler_bytes:
                raise SystemExit(
                    "%s carries a different code/inference.py than "
                    "Deploy/sagemaker/inference.py. The endpoints serve the "
                    "packaged one and this image would score with the repository "
                    "one, so their predictions could differ. Re-run "
                    "build_artifacts.py, then re-upload." % tar_path
                )

            for member in ARTIFACT_MEMBERS:
                if member not in names:
                    if member == "calibration.json":
                        continue  # LGD is uncalibrated; its manifest says so.
                    raise SystemExit("%s has no %s" % (tar_path, member))
                extracted = tar.extractfile(member)
                destination = os.path.join(target, member)
                with open(destination, "wb") as out:
                    shutil.copyfileobj(extracted, out)
                entries.append({
                    "path": "%s/%s" % (target_relative, member),
                    "sha256": digest(destination),
                    "bytes": os.path.getsize(destination),
                    "source": "Deploy/sagemaker/dist/%s/model.tar.gz:%s" % (bundle, member),
                })

    # The handler itself, imported by riskforge/scoring_local.py. Named
    # inference_handler.py inside the package rather than left at inference.py so
    # that it cannot shadow anything and so its origin is obvious from the import.
    target = os.path.join(BUILD, "riskforge", "inference_handler.py")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(handler_source, target)
    entries.append({
        "path": "riskforge/inference_handler.py",
        "sha256": digest(target),
        "bytes": os.path.getsize(target),
        "source": "Deploy/sagemaker/inference.py",
    })


def main():
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(BUILD)

    entries = []
    for relative in REPO_FILES:
        stage_one(REPO, relative, entries)
    for relative in LOCAL_FILES:
        stage_one(HERE, relative, entries)
    stage_endpoint_artifacts(entries)

    # A file that should never be in the image is worth failing the build over
    # rather than trusting the list above to stay right.
    for root, _, files in os.walk(BUILD):
        for name in files:
            if name == ".env" or name.endswith((".db", ".sqlite", ".pem", ".csv", ".parquet")):
                raise SystemExit("refusing to stage %s" % os.path.join(root, name))

    manifest = os.path.join(BUILD, "MANIFEST.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"files": entries}, f, indent=2, sort_keys=True)

    total = sum(e["bytes"] for e in entries)
    for entry in entries:
        print("  %8.1f KB  %s" % (entry["bytes"] / 1024, entry["path"]))
    print("staged %d files, %.1f MB, into %s" % (len(entries), total / 1024 / 1024, BUILD))
    return 0


if __name__ == "__main__":
    sys.exit(main())
