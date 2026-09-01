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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.join(HERE, "build")

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


def main():
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(BUILD)

    entries = []
    for relative in REPO_FILES:
        stage_one(REPO, relative, entries)
    for relative in LOCAL_FILES:
        stage_one(HERE, relative, entries)

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
