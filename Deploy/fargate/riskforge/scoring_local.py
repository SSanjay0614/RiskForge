"""
The PD and LGD models, in this process instead of over the network.

This is the same calculation riskforge/scoring.py performs and it exists for one
reason: shape of traffic. The endpoint path splits the frame into blocks a
Serverless request body can hold and sends them, so the whole portfolio is
878,317 rows / 2,000 per block = 440 requests per model, 880 in total, throttled
to the account's total serverless concurrency of five. The endpoints themselves
are fast -- ModelLatency measures 78-177 ms -- and that is the problem: almost
none of the elapsed time is inference. It is 880 round trips and 176 sequential
waves of them, plus a cold start on the first.

Nothing about the numbers changes here, and that is not a claim about care taken
-- it is structural. This module does not reimplement inference:

  * The **handler is the deployed handler.** stage.py copies
    Deploy/sagemaker/inference.py into the image and asserts it is byte-identical
    to the code/inference.py inside both model.tar.gz bundles, so the alignment,
    the pinned iteration_range, the isotonic interpolation and the tier cutoffs
    are executed by the same source that executes them on the endpoint.
  * The **artifacts are the deployed artifacts**, extracted from those same
    tarballs -- the booster JSON, the 227 isotonic knots and the manifest,
    byte-for-byte what SageMaker serves from S3.

So the endpoint and this module differ in transport and in nothing else. What
that buys is the 880 requests collapsing into two `Booster.predict` calls.

**The endpoint path is not deleted.** riskforge/scoring.py stays, the endpoints
stay deployed, and task.py chooses between them with --scoring. Bulk scoring
belongs here; a single loan scored on demand from the interface belongs on an
endpoint that costs nothing while idle, and keeping both means the pipeline can
be pointed back at the endpoints by changing one argument if this path ever
disagrees with them.

Predictions are computed in row slices rather than one array. Not for
correctness -- feature engineering and both boosters are strictly row-wise, so
any slicing gives identical output -- but because predict_fn returns Python
floats, and 878,317 of them in a list is ~70 MB of objects per model. Slicing
bounds that to the slice while the frame itself is read whole.
"""
import os
import time

import joblib
import numpy as np
import pandas as pd

from dmodels.expected_loss_result import ExpectedLossResult

from config import LGD_FEATURE_NAMES_PATH, PD_FEATURE_NAMES_PATH

from utils.logger import logger

from . import inference_handler
from .scoring import EndpointError

# config.py derives its own paths from its own location, so this is /app/Models in
# the image and <repo>/Models from a checkout -- the same directory the frequency
# maps and feature-name lists already come from. Deriving it from a path config
# already exports rather than recomputing it keeps one definition of where the
# model files are.
MODELS_DIR = os.path.dirname(os.path.abspath(PD_FEATURE_NAMES_PATH))

PD_ARTIFACT_DIR = os.path.join(MODELS_DIR, "pd-endpoint")
LGD_ARTIFACT_DIR = os.path.join(MODELS_DIR, "lgd-endpoint")

# Rows per predict_fn call. 250,000 keeps the returned Python float list around
# 20 MB while making the number of calls four, not four hundred.
DEFAULT_PREDICT_ROWS = 250_000

# Loaded once per process and held. On Fargate that is once per task and means
# nothing; on Lambda it is once per execution environment, so every warm
# invocation after the first skips both loads entirely. Together the two boosters
# are 1.2 MB of JSON, so this is milliseconds either way -- the reason to cache is
# that a per-batch load would be four loads of the same file.
_models = {}


def _model(kind):
    """The manifest dict inference.model_fn builds, with the booster inside it."""
    if kind not in _models:
        directory = PD_ARTIFACT_DIR if kind == "pd" else LGD_ARTIFACT_DIR
        manifest_path = os.path.join(directory, "manifest.json")
        if not os.path.exists(manifest_path):
            raise EndpointError(
                "no %s model artifact at %s. stage.py extracts these from "
                "Deploy/sagemaker/dist/%s-model/model.tar.gz, so either "
                "build_artifacts.py has not been run or the image predates "
                "in-process scoring -- run with --scoring endpoint to use the "
                "SageMaker endpoints instead." % (kind.upper(), directory, kind)
            )
        started = time.time()
        _models[kind] = inference_handler.model_fn(directory)
        logger.info(
            "scoring_local | loaded %s artifact from %s in %.2fs"
            % (kind.upper(), directory, time.time() - started)
        )
    return _models[kind]


class LocalScorer:
    """
    riskforge.scoring.Scorer's contract, computed in this process.

    Same constructor shape so task.py and credit.py can hold either one, and the
    endpoint names are accepted and recorded rather than used -- the output
    envelope names which models produced the numbers, and "the PD endpoint's
    artifact, run locally" is the honest answer to that.
    """

    def __init__(self, pd_endpoint=None, lgd_endpoint=None,
                 predict_rows=DEFAULT_PREDICT_ROWS, **ignored):
        self.pd_endpoint = pd_endpoint
        self.lgd_endpoint = lgd_endpoint
        self.predict_rows = int(predict_rows or DEFAULT_PREDICT_ROWS)

        # The same two lists the endpoint path sends columns from, read from the
        # same artifacts. Selecting here as well as inside the handler is the
        # double alignment scoring.py documents: this turns a feature the
        # engineering step failed to produce into an exception naming it, and the
        # handler's filled_features turns a disagreement between the two into a
        # failure rather than a plausible number.
        self.pd_features = list(joblib.load(PD_FEATURE_NAMES_PATH))
        self.lgd_features = list(joblib.load(LGD_FEATURE_NAMES_PATH))

    def _select(self, df, feature_names, model_name):
        missing = [f for f in feature_names if f not in df.columns]
        if missing:
            raise EndpointError(
                "%s model needs %d feature(s) the engineered frame does not have: "
                "%s. Feature engineering produced %d columns; this is a mismatch "
                "between the engineering step and the model artifact, not "
                "something to fill with zeros."
                % (model_name, len(missing), missing[:12], len(df.columns))
            )
        return df[feature_names]

    def _predict(self, kind, matrix, want_tiers):
        """Every row through the deployed handler, in slices, in row order."""
        model = _model(kind)
        columns = list(matrix.columns)
        # object dtype would mean a column the engineering step left as text.
        # Converting once, whole, keeps the per-slice work to a view.
        values = matrix.to_numpy(dtype=np.float64)

        total = values.shape[0]
        step = max(1, self.predict_rows)
        predictions, tiers = [], []
        started = time.time()

        for start in range(0, total, step):
            block = values[start:start + step]
            body = inference_handler.predict_fn((columns, block), model)

            filled = body.get("filled_features") or []
            if filled:
                raise EndpointError(
                    "%s model filled %d feature(s) with 0 rather than reading them "
                    "from the frame: %s. The frame was built from the model's own "
                    "feature list, so this means the artifact expects features that "
                    "list does not name -- train/serve skew, and the predictions "
                    "would be wrong without any error."
                    % (kind.upper(), len(filled), filled[:12])
                )

            block_predictions = body.get("predictions")
            if not isinstance(block_predictions, list) or len(block_predictions) != len(block):
                raise EndpointError(
                    "%s model returned %r predictions for %d rows"
                    % (kind.upper(),
                       len(block_predictions) if isinstance(block_predictions, list)
                       else block_predictions,
                       len(block))
                )
            predictions.extend(block_predictions)

            block_tiers = body.get("risk_tiers") or []
            if want_tiers:
                if len(block_tiers) != len(block):
                    raise EndpointError(
                        "%s model returned %d risk tiers for %d rows"
                        % (kind.upper(), len(block_tiers), len(block))
                    )
                tiers.extend(block_tiers)

        logger.info(
            "scoring_local | %s | %d rows in %d call(s), %.1fs"
            % (kind.upper(), total, (total + step - 1) // step, time.time() - started)
        )
        return np.asarray(predictions, dtype=np.float64), tiers

    def score(self, loans):
        """
        ExpectedLossResult, field for field what riskforge.scoring.Scorer.score
        returns -- including the total_exposure > 0 guards, which are the local
        tool's and are there because a query can legitimately retrieve loans whose
        outstanding balance is zero.
        """
        if "exposure_at_default" not in loans.columns:
            raise ValueError("loans must include an 'exposure_at_default' column")

        X_pd = self._select(loans, self.pd_features, "PD")
        X_lgd = self._select(loans, self.lgd_features, "LGD")

        predicted_pd, risk_tiers = self._predict("pd", X_pd, want_tiers=True)
        predicted_lgd, _ = self._predict("lgd", X_lgd, want_tiers=False)

        # The handler clips LGD to [0, 1] because manifest clip_to_unit is true, so
        # this cannot fire unless the staged artifact's manifest says otherwise.
        # Kept because that is exactly the condition worth hearing about, and
        # re-clipping here would hide it.
        out_of_range = int(((predicted_lgd < 0.0) | (predicted_lgd > 1.0)).sum())
        if out_of_range:
            raise EndpointError(
                "%d LGD prediction(s) fell outside [0, 1]. The handler clips when "
                "the manifest says clip_to_unit, so this means the staged LGD "
                "artifact is not the one build_artifacts.py wrote." % out_of_range
            )

        ead = loans["exposure_at_default"].to_numpy(dtype=np.float64)
        expected_loss_per_loan = predicted_pd * predicted_lgd * ead

        total_exposure = float(ead.sum())
        total_expected_loss = float(expected_loss_per_loan.sum())

        scored_df = loans.copy()
        scored_df["predicted_pd"] = predicted_pd
        scored_df["predicted_lgd"] = predicted_lgd
        scored_df["risk_tier"] = risk_tiers
        scored_df["expected_loss"] = expected_loss_per_loan

        return ExpectedLossResult(
            loan_count=len(loans),
            total_exposure=total_exposure,
            total_expected_loss=total_expected_loss,
            expected_loss_rate=(
                total_expected_loss / total_exposure if total_exposure > 0 else 0.0
            ),
            exposure_weighted_avg_pd=(
                float((predicted_pd * ead).sum() / total_exposure)
                if total_exposure > 0 else 0.0
            ),
            exposure_weighted_avg_lgd=(
                float((predicted_lgd * ead).sum() / total_exposure)
                if total_exposure > 0 else 0.0
            ),
            # normalize=True to match ExpectedLossTool exactly: shares of the loan
            # count, not of exposure.
            risk_tier_distribution=pd.Series(risk_tiers).value_counts(normalize=True).to_dict(),
            scored_df=scored_df,
        )
