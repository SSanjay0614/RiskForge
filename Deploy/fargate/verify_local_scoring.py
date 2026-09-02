"""
Proves the in-process scoring path produces the endpoints' numbers, exactly.

    python Deploy/fargate/verify_local_scoring.py

Deploy/sagemaker/verify_endpoints.py does this for the deployed endpoints: it
replays 64 fixed rows through them and compares against
dist/reference_vectors.json, whose expected values were computed at build time
from the .joblib models the notebooks produced. This is the same check against
the same vectors, aimed at riskforge/scoring_local.py instead -- so "the Lambda
scores what the notebook scored" is a passing test rather than an argument.

Zero tolerance, for the same reason build_artifacts.py uses zero: anything above
it would be a threshold chosen to make a real difference acceptable. The
artifacts are the same bytes and the handler is the same file, so the only
correct result is bit-for-bit.

Run this before pushing an image. It needs Deploy/fargate/build/ to exist, which
means stage.py has been run, which means the artifacts came out of the tarballs
that were uploaded to S3.
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.join(HERE, "build")
VECTORS = os.path.join(REPO, "Deploy", "sagemaker", "dist", "reference_vectors.json")

# The staged build context is what the image contains, so it is what gets tested
# -- not the repository copies of the same files. A mismatch between the two is
# exactly the kind of thing this should catch rather than skip past.
sys.path.insert(0, BUILD)


def main():
    if not os.path.isdir(BUILD):
        raise SystemExit(
            "no %s -- run `python Deploy/fargate/stage.py` first." % BUILD)
    if not os.path.exists(VECTORS):
        raise SystemExit(
            "no %s -- run `python Deploy/sagemaker/build_artifacts.py` first." % VECTORS)

    from riskforge import inference_handler

    vectors = json.load(open(VECTORS))
    failures = []

    for kind, directory in (("pd", "pd-endpoint"), ("lgd", "lgd-endpoint")):
        model = inference_handler.model_fn(os.path.join(BUILD, "Models", directory))
        case = vectors[kind]

        # Through predict_fn, not straight to the booster: the alignment, the
        # pinned iteration_range, the isotonic interpolation and the clip are the
        # parts most able to differ, and they all live in there.
        body = inference_handler.predict_fn((case["columns"], np.asarray(case["data"])), model)

        got = np.asarray(body["predictions"], dtype=np.float64)
        want = np.asarray(case["expected_predictions"], dtype=np.float64)

        if body["filled_features"]:
            failures.append("%s: handler filled %d feature(s): %s"
                            % (kind.upper(), len(body["filled_features"]),
                               body["filled_features"][:8]))

        identical = np.array_equal(got, want)
        worst = float(np.max(np.abs(got - want))) if len(got) == len(want) else float("nan")
        print("%-4s %d rows | max abs diff %.3e | %s"
              % (kind.upper(), len(got), worst, "identical" if identical else "DIFFERENT"))

        if not identical:
            index = int(np.argmax(np.abs(got - want)))
            failures.append(
                "%s: row %d expected %.17g, got %.17g"
                % (kind.upper(), index, want[index], got[index]))

        if kind == "pd":
            tiers = body.get("risk_tiers") or []
            if len(tiers) != len(got):
                failures.append("PD: %d risk tiers for %d rows" % (len(tiers), len(got)))
            else:
                print("PD   risk tiers assigned from the artifact's own cutoffs: %s"
                      % sorted(set(tiers)))

    if failures:
        print()
        for line in failures:
            print("FAIL  %s" % line)
        return 1

    print("\nIn-process scoring matches the notebook models bit-for-bit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
