"""
Replays fixed reference vectors against the live PD and LGD endpoints and fails
on any difference from what the joblib models produce locally.

    python Deploy/sagemaker/verify_endpoints.py --profile riskforge

This is the check that makes the repackaging in build_artifacts.py trustworthy
rather than merely plausible. That script proves the JSON booster reproduces the
notebook model under the *local* xgboost, 2.1.4. The endpoints run SageMaker's
prebuilt image, and no 2.x image exists -- 3.0-5 is what serves these models. A
newer XGBoost reading an older model file is supported, but "supported" is not
"identical", and a silently shifted PD becomes a silently wrong Expected Loss.
So the comparison is made against the deployed thing, at zero tolerance.

It also exercises the parts of the contract a happy-path smoke test would not:
that a column the model does not use is ignored rather than shifting the
alignment, that an absent feature is reported in filled_features instead of
being filled in silence, and that the risk tiers come back with the PD.
"""
import argparse
import json
import os
import sys

import boto3
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
VECTORS = os.path.join(HERE, "dist", "reference_vectors.json")

TOLERANCE = 0.0


def invoke(runtime, endpoint, payload):
    response = runtime.invoke_endpoint(
        EndpointName=endpoint,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps(payload).encode("utf-8"),
    )
    return json.loads(response["Body"].read())


def check(runtime, endpoint, block, label, expect_tiers):
    payload = {"columns": block["columns"], "data": block["data"]}
    body = invoke(runtime, endpoint, payload)

    expected = np.asarray(block["expected_predictions"], dtype=np.float64)
    actual = np.asarray(body["predictions"], dtype=np.float64)

    problems = []
    if actual.shape != expected.shape:
        problems.append(f"returned {actual.shape[0]} predictions for {expected.shape[0]} rows")
    else:
        drift = float(np.abs(actual - expected).max())
        print(f"{label:<4} {actual.shape[0]} rows | max |difference| = {drift:.3e}")
        if drift > TOLERANCE:
            worst = int(np.argmax(np.abs(actual - expected)))
            problems.append(
                f"prediction drift {drift:.3e} (row {worst}: "
                f"endpoint {actual[worst]!r} vs local {expected[worst]!r})"
            )

    if body.get("filled_features"):
        problems.append(f"endpoint filled {body['filled_features']} with zeros")
    if body.get("unused_features"):
        problems.append(f"endpoint ignored supplied columns {body['unused_features']}")

    if expect_tiers:
        tiers = body.get("risk_tiers")
        if not tiers:
            problems.append("no risk_tiers in the response")
        elif len(tiers) != actual.shape[0]:
            problems.append("risk_tiers length does not match predictions")
        else:
            counts = {t: tiers.count(t) for t in sorted(set(tiers))}
            print(f"{label:<4} risk tiers: {counts}")
    elif body.get("risk_tiers"):
        problems.append("LGD endpoint returned risk tiers, which it has no cutoffs for")

    return problems


def check_alignment_is_by_name(runtime, endpoint, block, label):
    """Reverse the column order, add a column the model does not know, drop one it
    does. Predictions must be unchanged for the reversal, and the dropped feature
    must be named in filled_features rather than passed over in silence."""
    columns = list(block["columns"])
    data = [list(row) for row in block["data"]]

    reversed_payload = {
        "columns": list(reversed(columns)),
        "data": [list(reversed(row)) for row in data],
    }
    reversed_out = np.asarray(invoke(runtime, endpoint, reversed_payload)["predictions"])
    forward_out = np.asarray(block["expected_predictions"], dtype=np.float64)

    problems = []
    drift = float(np.abs(reversed_out - forward_out).max())
    print(f"{label:<4} column order reversed | max |difference| = {drift:.3e}")
    if drift > TOLERANCE:
        problems.append(f"{label}: reversing column order changed predictions by {drift:.3e}")

    dropped = columns[0]
    partial = {
        "columns": columns[1:] + ["a_column_the_model_never_saw"],
        "data": [row[1:] + [1.234] for row in data],
    }
    partial_out = invoke(runtime, endpoint, partial)
    if partial_out.get("filled_features") != [dropped]:
        problems.append(
            f"{label}: dropping {dropped} reported filled_features="
            f"{partial_out.get('filled_features')!r}, expected [{dropped!r}]"
        )
    else:
        print(f"{label:<4} missing feature reported: {partial_out['filled_features']}")
    if partial_out.get("unused_features") != ["a_column_the_model_never_saw"]:
        problems.append(
            f"{label}: unknown column reported as {partial_out.get('unused_features')!r}"
        )
    else:
        print(f"{label:<4} unknown column ignored: {partial_out['unused_features']}")

    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None, help="AWS profile; omit on the EC2 host")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--pd-endpoint", default="riskforge-pd-endpoint")
    parser.add_argument("--lgd-endpoint", default="riskforge-lgd-endpoint")
    args = parser.parse_args()

    if not os.path.exists(VECTORS):
        raise SystemExit(f"{VECTORS} is missing -- run build_artifacts.py first")
    with open(VECTORS) as f:
        vectors = json.load(f)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    runtime = session.client("sagemaker-runtime")

    problems = []
    problems += check(runtime, args.pd_endpoint, vectors["pd"], "PD", expect_tiers=True)
    problems += check(runtime, args.lgd_endpoint, vectors["lgd"], "LGD", expect_tiers=False)
    problems += check_alignment_is_by_name(runtime, args.pd_endpoint, vectors["pd"], "PD")

    print()
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        sys.exit(1)
    print("Both endpoints reproduce the local models exactly.")


if __name__ == "__main__":
    main()
