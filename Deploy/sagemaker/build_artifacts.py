"""
Turns the trained joblib models into the two model.tar.gz bundles SageMaker
serves, and refuses to write either one unless it first proves the repackaged
artifact scores identically to the model the notebooks produced.

    python Deploy/sagemaker/build_artifacts.py

Writes (all under Deploy/sagemaker/dist/, which is gitignored -- the tarballs are
build output, not source):

    dist/pd-model/model.tar.gz     booster + isotonic knots + tier cutoffs + code/
    dist/lgd-model/model.tar.gz    booster + code/
    dist/reference_vectors.json    64 fixed rows and the expected outputs, used to
                                   verify the deployed endpoints numerically

Why repackage at all, rather than shipping the .joblib files:

  * A pickle must be unpickled by a compatible scikit-learn. These were trained
    under scikit-learn 1.6.1 / xgboost 2.1.4; the SageMaker XGBoost containers
    offer 1.7 and 3.x, and no 2.x at all. Pinning the training versions inside the
    container would mean a pip install on every cold start -- on a serverless
    endpoint that is paid for twice, in latency and in dollars.
  * Unpickling executes code by design. An artifact bucket is a smaller target
    than a serving path that will run whatever the artifact says.

XGBoost's JSON model format has neither problem: it is data, and newer XGBoost
reads older models. The scikit-learn half of the PD model -- an isotonic
calibration curve -- is 227 (x, y) knots, so it travels as JSON too, and
np.interp reproduces IsotonicRegression(out_of_bounds="clip") exactly.
"""
import io
import json
import os
import shutil
import tarfile

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
MODELS_DIR = os.path.join(REPO_ROOT, "Models")
DIST = os.path.join(HERE, "dist")

REFERENCE_ROWS = 64
REFERENCE_SEED = 20260831
TOLERANCE = 0.0  # bit-for-bit; anything above zero would be hiding a real change


def _load_source_models():
    pd_calibrated = joblib.load(os.path.join(MODELS_DIR, "pd_model_calibrated.joblib"))
    lgd = joblib.load(os.path.join(MODELS_DIR, "lgd_model.joblib"))
    return {
        "pd_calibrated": pd_calibrated,
        "pd_features": joblib.load(os.path.join(MODELS_DIR, "pd_model_feature_names.joblib")),
        "pd_cutoffs": joblib.load(os.path.join(MODELS_DIR, "pd_risk_tier_cutoffs.joblib")),
        "lgd": lgd,
        "lgd_features": joblib.load(os.path.join(MODELS_DIR, "lgd_model_feature_names.joblib")),
    }


def _isotonic_knots(pd_calibrated):
    """The calibration curve, as data.

    Asserted rather than assumed: CalibratedClassifierCV(method="isotonic")
    fits IsotonicRegression(out_of_bounds="clip"), and np.interp with the ends
    clamped is that function. A sigmoid calibrator, or a non-clipping one, would
    need different serving code, so this stops instead of quietly emitting knots
    that mean something else.
    """
    calibrated = pd_calibrated.calibrated_classifiers_
    if len(calibrated) != 1:
        raise SystemExit(f"expected one prefit calibrator, found {len(calibrated)}")

    inner = calibrated[0]
    calibrators = getattr(inner, "calibrators", None) or inner.calibrators_
    if len(calibrators) != 1:
        raise SystemExit(f"expected one calibrator per class pair, found {len(calibrators)}")

    curve = calibrators[0]
    if type(curve).__name__ != "IsotonicRegression":
        raise SystemExit(f"calibrator is {type(curve).__name__}, not IsotonicRegression")
    if getattr(curve, "out_of_bounds", None) != "clip":
        raise SystemExit(f"out_of_bounds is {curve.out_of_bounds!r}, expected 'clip'")
    if getattr(curve, "increasing_", True) is not True:
        raise SystemExit("calibration curve is decreasing; serving code assumes increasing")

    estimator = getattr(inner, "estimator", None) or inner.base_estimator
    return estimator, {
        "x_thresholds": [float(v) for v in curve.X_thresholds_],
        "y_thresholds": [float(v) for v in curve.y_thresholds_],
    }


def _export_booster(sk_model, feature_names, out_path, label):
    booster = sk_model.get_booster()

    # The saved feature-name list and the booster's own must agree. If they ever
    # diverge, the pairing of artifacts is wrong and every prediction downstream
    # is scored against the wrong columns -- with no error anywhere.
    if list(booster.feature_names or []) != list(feature_names):
        raise SystemExit(
            f"{label}: booster feature names do not match "
            f"{label.lower()}_model_feature_names.joblib"
        )

    booster.save_model(out_path)
    total = booster.num_boosted_rounds()
    best = booster.attributes().get("best_iteration")
    rounds = total if best is None else int(best) + 1
    return {"iteration_range": [0, rounds], "n_features": len(feature_names)}


def _reference_frame(feature_names, seed):
    """Fixed pseudo-random rows, wide enough in range to exercise the calibration
    curve's clipped ends as well as its middle."""
    rng = np.random.default_rng(seed)
    values = rng.normal(scale=3.0, size=(REFERENCE_ROWS, len(feature_names)))
    return pd.DataFrame(values, columns=list(feature_names))


def _write_bundle(target_dir, files, manifest):
    """Assemble one model.tar.gz. code/ holds the handler; SageMaker is pointed at
    it with SAGEMAKER_SUBMIT_DIRECTORY=/opt/ml/model/code."""
    staging = os.path.join(target_dir, "staging")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(os.path.join(staging, "code"))

    for name, blob in files.items():
        mode = "wb" if isinstance(blob, bytes) else "w"
        with open(os.path.join(staging, name), mode) as f:
            f.write(blob)

    with open(os.path.join(staging, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    shutil.copy(os.path.join(HERE, "inference.py"), os.path.join(staging, "code", "inference.py"))

    tar_path = os.path.join(target_dir, "model.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        for entry in sorted(os.listdir(staging)):
            tar.add(os.path.join(staging, entry), arcname=entry)

    shutil.rmtree(staging)
    return tar_path


def main():
    src = _load_source_models()
    inner_pd, knots = _isotonic_knots(src["pd_calibrated"])

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    pd_dir = os.path.join(DIST, "pd-model")
    lgd_dir = os.path.join(DIST, "lgd-model")
    os.makedirs(pd_dir)
    os.makedirs(lgd_dir)

    pd_booster_path = os.path.join(pd_dir, "booster.json")
    lgd_booster_path = os.path.join(lgd_dir, "booster.json")
    pd_meta = _export_booster(inner_pd, src["pd_features"], pd_booster_path, "PD")
    lgd_meta = _export_booster(src["lgd"], src["lgd_features"], lgd_booster_path, "LGD")

    # --- equivalence check, before anything is packed --------------------------
    # The reference is the model as the application calls it today. If the
    # repackaged path disagrees by any amount at all, the build stops: a
    # "negligible" difference in a PD feeds straight into an Expected Loss figure.
    X_pd = _reference_frame(src["pd_features"], REFERENCE_SEED)
    X_lgd = _reference_frame(src["lgd_features"], REFERENCE_SEED + 1)

    expected_pd = src["pd_calibrated"].predict_proba(X_pd)[:, 1]
    expected_lgd = src["lgd"].predict(X_lgd).clip(0, 1)

    replay_pd_raw = xgb.Booster(model_file=pd_booster_path).predict(
        xgb.DMatrix(X_pd), iteration_range=tuple(pd_meta["iteration_range"])
    )
    x = np.asarray(knots["x_thresholds"], dtype=np.float64)
    y = np.asarray(knots["y_thresholds"], dtype=np.float64)
    replay_pd = np.interp(np.clip(replay_pd_raw, x[0], x[-1]), x, y)

    replay_lgd = xgb.Booster(model_file=lgd_booster_path).predict(
        xgb.DMatrix(X_lgd), iteration_range=tuple(lgd_meta["iteration_range"])
    ).clip(0, 1)

    for label, expected, replay in (("PD", expected_pd, replay_pd), ("LGD", expected_lgd, replay_lgd)):
        drift = float(np.abs(np.asarray(replay, dtype=np.float64) - expected).max())
        if drift > TOLERANCE:
            raise SystemExit(f"{label}: repackaged model differs from source by {drift:.3e}")
        print(f"{label:<4} repackaged model matches the source exactly ({REFERENCE_ROWS} rows)")

    # --- bundles ---------------------------------------------------------------
    with open(pd_booster_path, "rb") as f:
        pd_booster_blob = f.read()
    with open(lgd_booster_path, "rb") as f:
        lgd_booster_blob = f.read()
    os.remove(pd_booster_path)
    os.remove(lgd_booster_path)

    pd_tar = _write_bundle(
        pd_dir,
        {
            "booster.json": pd_booster_blob,
            "calibration.json": json.dumps(knots),
            "feature_names.json": json.dumps(list(src["pd_features"]), indent=2),
        },
        {
            "model": "riskforge-pd",
            "booster_file": "booster.json",
            "calibration_file": "calibration.json",
            "feature_names": list(src["pd_features"]),
            "iteration_range": pd_meta["iteration_range"],
            "risk_tier_cutoffs": {k: float(v) for k, v in src["pd_cutoffs"].items()},
            "clip_to_unit": False,  # isotonic output is already within [0, 1]
            "trained_with": {"xgboost": xgb.__version__, "calibration": "isotonic/prefit"},
        },
    )
    lgd_tar = _write_bundle(
        lgd_dir,
        {
            "booster.json": lgd_booster_blob,
            "feature_names.json": json.dumps(list(src["lgd_features"]), indent=2),
        },
        {
            "model": "riskforge-lgd",
            "booster_file": "booster.json",
            "calibration_file": None,
            "feature_names": list(src["lgd_features"]),
            "iteration_range": lgd_meta["iteration_range"],
            # A regressor has no bound of its own; LGD is a fraction of exposure,
            # so a prediction outside [0, 1] is meaningless and gets clamped --
            # matching ExpectedLossTool.run, which has always clipped.
            "clip_to_unit": True,
            "trained_with": {"xgboost": xgb.__version__, "calibration": None},
        },
    )

    with open(os.path.join(DIST, "reference_vectors.json"), "w") as f:
        json.dump(
            {
                "note": "expected outputs come from the joblib models, computed at build time",
                "seed": REFERENCE_SEED,
                "pd": {
                    "columns": list(src["pd_features"]),
                    "data": X_pd.to_numpy().tolist(),
                    "expected_predictions": [float(v) for v in expected_pd],
                },
                "lgd": {
                    "columns": list(src["lgd_features"]),
                    "data": X_lgd.to_numpy().tolist(),
                    "expected_predictions": [float(v) for v in expected_lgd],
                },
            },
            f,
        )

    for path in (pd_tar, lgd_tar):
        print(f"{os.path.getsize(path) / 1e6:>7.2f} MB  {path}")


if __name__ == "__main__":
    main()
