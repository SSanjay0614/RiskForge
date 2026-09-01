"""
SageMaker inference handler, shared by the PD and LGD endpoints.

One script serves both because the only differences are declared in the model
directory's manifest.json, not in code: which booster to load, how many trees to
use, whether an isotonic calibration curve is applied afterwards, and whether
the output carries risk tiers. Two near-identical files would drift.

Nothing here unpickles anything. The artifacts are XGBoost's own JSON model
format plus small JSON sidecars, so serving does not depend on the scikit-learn
or joblib versions that happened to be installed when the models were trained --
and a compromised artifact cannot execute code on load the way a pickle can.
The reasoning, and the proof that this reproduces the notebook's numbers
exactly, is in Deploy/sagemaker/README.md.

Contract, both endpoints:

  request  (application/json), either shape:
      {"columns": ["loan_amnt", ...], "data": [[...], [...]]}   preferred
      {"instances": [{"loan_amnt": 1.0, ...}, ...]}
  response (application/json):
      {"predictions": [...],          # PD: calibrated probability. LGD: [0,1].
       "risk_tiers": [...],           # PD only
       "row_count": n,
       "filled_features": [...],      # features absent from the request, sent as 0
       "unused_features": [...]}      # columns supplied that this model ignores
"""
import json
import os

import numpy as np
import xgboost as xgb

JSON = "application/json"


def model_fn(model_dir):
    """Load once per container, not per request."""
    with open(os.path.join(model_dir, "manifest.json")) as f:
        manifest = json.load(f)

    booster = xgb.Booster()
    booster.load_model(os.path.join(model_dir, manifest["booster_file"]))

    # Pinned explicitly rather than left to the library's default. Booster.predict
    # decides how many trees to use from the model's own best_iteration attribute,
    # and that default has changed between XGBoost majors -- which would move
    # every prediction silently on a container upgrade. The value written at build
    # time is the one the notebook scored with.
    manifest["iteration_range"] = tuple(manifest["iteration_range"])

    if manifest.get("calibration_file"):
        with open(os.path.join(model_dir, manifest["calibration_file"])) as f:
            cal = json.load(f)
        manifest["cal_x"] = np.asarray(cal["x_thresholds"], dtype=np.float64)
        manifest["cal_y"] = np.asarray(cal["y_thresholds"], dtype=np.float64)

    manifest["booster"] = booster
    return manifest


def input_fn(request_body, content_type=JSON):
    if content_type != JSON:
        raise ValueError(f"unsupported content type {content_type!r}; send {JSON}")

    payload = json.loads(request_body)

    if "columns" in payload and "data" in payload:
        columns = list(payload["columns"])
        rows = payload["data"]
        width = len(columns)
        for i, row in enumerate(rows):
            if len(row) != width:
                raise ValueError(
                    f"row {i} has {len(row)} values but {width} columns were declared"
                )
        return columns, np.asarray(rows, dtype=np.float64).reshape(len(rows), width)

    if "instances" in payload:
        records = payload["instances"]
        if not records:
            raise ValueError("'instances' is empty")
        # Column order is taken from the first record and then enforced on the
        # rest by key lookup, so a record that happens to serialise its keys in a
        # different order cannot shift a value into the wrong feature.
        columns = list(records[0].keys())
        matrix = np.empty((len(records), len(columns)), dtype=np.float64)
        for i, record in enumerate(records):
            missing = [c for c in columns if c not in record]
            if missing:
                raise ValueError(f"instance {i} is missing keys {missing}")
            for j, column in enumerate(columns):
                matrix[i, j] = record[column]
        return columns, matrix

    raise ValueError("body must contain either 'columns'+'data' or 'instances'")


def predict_fn(request, model):
    supplied_columns, matrix = request
    expected = model["feature_names"]

    index = {name: j for j, name in enumerate(supplied_columns)}

    # Selected strictly by name, never by position. The two models share most of
    # their features but not their order or their count (PD has 68, LGD 40), so
    # trusting the caller's column order is how a rate column ends up scored as a
    # balance -- an error that produces plausible numbers and no exception.
    aligned = np.zeros((matrix.shape[0], len(expected)), dtype=np.float64)
    filled = []
    for j, name in enumerate(expected):
        if name in index:
            aligned[:, j] = matrix[:, index[name]]
        else:
            filled.append(name)

    if not np.isfinite(aligned).all():
        # XGBoost treats NaN as a missing value and would score the row happily.
        # For engineered features an NaN means the upstream transform failed, so
        # failing loudly here is the point.
        bad = int(np.argwhere(~np.isfinite(aligned))[0][0])
        raise ValueError(f"row {bad} contains NaN or infinity after alignment")

    dmatrix = xgb.DMatrix(aligned, feature_names=expected)
    raw = model["booster"].predict(dmatrix, iteration_range=model["iteration_range"])

    if "cal_x" in model:
        # Exactly what sklearn's IsotonicRegression(out_of_bounds="clip") does:
        # linear interpolation between the fitted knots, clamped at both ends.
        x, y = model["cal_x"], model["cal_y"]
        predictions = np.interp(np.clip(raw, x[0], x[-1]), x, y)
    else:
        predictions = raw.astype(np.float64)

    if model.get("clip_to_unit"):
        predictions = predictions.clip(0.0, 1.0)

    response = {
        "predictions": [float(p) for p in predictions],
        "row_count": int(matrix.shape[0]),
        "filled_features": filled,
        "unused_features": [c for c in supplied_columns if c not in set(expected)],
    }

    cutoffs = model.get("risk_tier_cutoffs")
    if cutoffs:
        # The cutoffs travel with the model artifact rather than living in the
        # caller, so a recalibration cannot leave a stale tier boundary behind in
        # a Lambda's source.
        response["risk_tiers"] = [_tier(p, cutoffs) for p in predictions]

    return response


def _tier(value, cutoffs):
    if value < cutoffs["Low"]:
        return "Low"
    if value < cutoffs["Medium"]:
        return "Medium"
    if value < cutoffs["High"]:
        return "High"
    return "Very High"


def output_fn(prediction, accept=JSON):
    if accept not in (JSON, "*/*", None, ""):
        raise ValueError(f"unsupported accept type {accept!r}; this endpoint returns {JSON}")
    return json.dumps(prediction), JSON
