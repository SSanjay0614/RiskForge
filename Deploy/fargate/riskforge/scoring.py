"""
The PD and LGD models, as two SageMaker Serverless endpoints.

This module replaces exactly one class -- tools/expected_loss_tool.py -- and
replaces it because that class does `joblib.load(PD_MODEL_PATH)`, and the point
of Phase 8 was that the models are endpoints now. Everything the class computed
after the two predict calls is reproduced here field for field, so
ExpectedLossResult comes back with the same meaning it has locally and
test_task.py can compare the two directly.

What changes, and why each change is the endpoint's answer rather than a
rewrite:

  * **Alignment happens twice, on purpose.** Deploy/sagemaker/inference.py aligns
    strictly by name and reports anything it had to fill in `filled_features`.
    The columns each model wants are also selected here, before the request, from
    the same feature-name artifacts. That is not redundancy for its own sake: the
    local selection turns a missing feature into an exception before a network
    call, and the endpoint's report turns a disagreement between the two into a
    failed check rather than a plausible number. `filled_features` non-empty
    fails the task -- ExpectedLossTool only logs a warning there, which is right
    for a notebook and wrong for a batch nobody is watching.
  * **Risk tiers come from the endpoint.** The PD artifact carries its own tier
    cutoffs and inference.py applies them, so re-deriving tiers here would put
    the boundary in two places. ExpectedLossTool._assign_risk_tier has no
    counterpart in this file for that reason.
  * **Calibration is already applied.** inference.py runs the isotonic map, so
    `predictions` from the PD endpoint is the calibrated probability -- the same
    number `pd_model.predict_proba(...)[:, 1]` gives locally, which
    Deploy/sagemaker/verify_endpoints.py checks at zero tolerance.

Batching is forced by the runtime, not chosen. A Serverless endpoint takes a
bounded request body and the whole portfolio is 878,000 rows, so the frame is
split, and the batches are sent concurrently up to the endpoint's own
max_concurrency -- five. Beyond that the endpoint queues, so more threads would
add latency and nothing else. Results are reassembled by batch index rather than
by completion order, because a portfolio scored in the wrong order is a portfolio
where every loan has some other loan's PD.
"""
import json
import threading
import time

import joblib
import numpy as np
import pandas as pd

from botocore.config import Config
from botocore.exceptions import ClientError

from dmodels.expected_loss_result import ExpectedLossResult

from config import LGD_FEATURE_NAMES_PATH, PD_FEATURE_NAMES_PATH

from utils.logger import logger

# A Serverless endpoint's request body limit is 4 MB. 3 MB leaves room for the
# JSON overhead the estimate below does not model exactly, and a batch that
# still comes out too large is split rather than rejected.
MAX_PAYLOAD_BYTES = 3 * 1024 * 1024

# About 800 bytes of JSON per row at 68 features, so 2,000 rows is ~1.6 MB --
# comfortably inside the limit with room for wider float representations.
DEFAULT_BATCH_ROWS = 2000

# Matched to the endpoints' max_concurrency, which Terraform passes in as WORKERS
# on the task definition -- so this literal is only the fallback for a container
# run by hand. Matching it is the whole tuning story: fewer leaves paid capacity
# idle, more only fills a queue on the endpoint's side.
#
# 5, and it is the account quota rather than a measurement. Service Quotas
# L-96300102, total concurrency across all serverless endpoints, is 10 on this
# account and PD and LGD are two endpoints. It was briefly set to 50 here, which
# is the right number for the workload -- 878,317 rows is hundreds of batches and
# at 5 concurrent most of the run is round-trip latency with the endpoint idle
# between -- and the apply that tried it was refused by SageMaker. Raise the quota
# first; infra/variables.tf carries the note.
DEFAULT_WORKERS = 5

# ModelNotReadyException is the cold start of a Serverless endpoint that has
# scaled to zero -- which is the steady state here, since scale-to-zero is why
# these endpoints cost nothing idle. It is expected on the first request of a
# run, not an error.
RETRYABLE = {
    "ThrottlingException", "ServiceUnavailable", "InternalFailure",
    "ModelNotReadyException", "InternalServerError", "RequestTimeout",
}
MAX_ATTEMPTS = 6
BACKOFF_BASE_S = 1.5

_local = threading.local()


def _runtime():
    """
    One client per thread. botocore clients are documented as thread-safe for
    calls, but the retry and connection-pool state is not worth sharing across
    the pool to save a handful of object constructions.
    """
    client = getattr(_local, "client", None)
    if client is None:
        import boto3
        client = boto3.client(
            "sagemaker-runtime",
            config=Config(
                # botocore's own retries are turned off: the loop below already
                # retries, and two nested backoffs turn a cold start into a
                # multi-minute wait that looks like a hang.
                retries={"max_attempts": 1, "mode": "standard"},
                # A Serverless cold start can take tens of seconds. The default
                # 60s read timeout would abandon it just as the container came up.
                read_timeout=180,
                connect_timeout=15,
                # 4, not the worker count. This client belongs to one thread and
                # that thread has one request outstanding at a time, so the pool
                # only ever needs one connection -- the old `DEFAULT_WORKERS + 2`
                # scaled the wrong thing, and at 50 workers it would have
                # allocated 52 idle connections per thread.
                max_pool_connections=4,
            ),
        )
        _local.client = client
    return client


def _batches(total, size):
    return [(start, min(start + size, total)) for start in range(0, total, size)]


class EndpointError(RuntimeError):
    """A call that failed in a way retrying will not fix, or ran out of retries."""


class Scorer:
    """
    Holds the two endpoint names and the two feature lists. Constructed once per
    task, because loading the feature lists per batch would be 440 reads of the
    same two files.
    """

    def __init__(self, pd_endpoint, lgd_endpoint, batch_rows=DEFAULT_BATCH_ROWS,
                 workers=DEFAULT_WORKERS):
        self.pd_endpoint = pd_endpoint
        self.lgd_endpoint = lgd_endpoint
        self.batch_rows = int(batch_rows)
        self.workers = int(workers)

        self.pd_features = list(joblib.load(PD_FEATURE_NAMES_PATH))
        self.lgd_features = list(joblib.load(LGD_FEATURE_NAMES_PATH))

    def _select(self, df, feature_names, model_name):
        """
        Strictly by name, and an absent feature is an exception rather than a
        zero fill.

        ExpectedLossTool fills and warns here. That is the right call in a
        notebook, where somebody reads the warning. In a task whose output is a
        JSON file, a warning nobody reads and a number that shifted are the same
        event, so this raises.
        """
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

    def _invoke(self, endpoint, columns, block):
        """One request, with retries on the errors that are worth retrying."""
        payload = json.dumps(
            {"columns": columns, "data": block},
            # NaN is not JSON and json.dumps emits a bare NaN token for it by
            # default, which the endpoint's json.loads accepts and float() turns
            # into a silent NaN prediction. There should be no NaN here at all --
            # FeatureEngineeringTool raises if any survive -- so this is the
            # assertion that says so at the boundary.
            allow_nan=False,
        ).encode("utf-8")

        if len(payload) > MAX_PAYLOAD_BYTES:
            return None  # the caller splits and retries

        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = _runtime().invoke_endpoint(
                    EndpointName=endpoint,
                    ContentType="application/json",
                    Accept="application/json",
                    Body=payload,
                )
                return json.loads(response["Body"].read().decode("utf-8"))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                last = exc
                if code not in RETRYABLE or attempt == MAX_ATTEMPTS:
                    raise EndpointError("%s: %s: %s" % (endpoint, code, exc)) from exc
                sleep = BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "scoring | %s | %s on attempt %d, retrying in %.1fs"
                    % (endpoint, code, attempt, sleep)
                )
                time.sleep(sleep)
        raise EndpointError("%s: exhausted retries: %s" % (endpoint, last))

    def _call_block(self, endpoint, columns, block, want_tiers):
        """
        A block of rows, split in half if the body is over the limit. Recursive
        rather than a pre-computed size, because the JSON width of a float is not
        known until it is written -- a column of long decimals makes rows that a
        row-count estimate says are fine.
        """
        body = self._invoke(endpoint, columns, block)
        if body is None:
            if len(block) == 1:
                raise EndpointError(
                    "%s: a single row exceeds the %d-byte request limit"
                    % (endpoint, MAX_PAYLOAD_BYTES)
                )
            half = len(block) // 2
            first = self._call_block(endpoint, columns, block[:half], want_tiers)
            second = self._call_block(endpoint, columns, block[half:], want_tiers)
            return {
                "predictions": first["predictions"] + second["predictions"],
                "risk_tiers": (first.get("risk_tiers") or []) + (second.get("risk_tiers") or []),
                "filled_features": sorted(
                    set(first.get("filled_features") or []) | set(second.get("filled_features") or [])
                ),
            }

        predictions = body.get("predictions")
        if not isinstance(predictions, list) or len(predictions) != len(block):
            raise EndpointError(
                "%s returned %r predictions for %d rows"
                % (endpoint, len(predictions) if isinstance(predictions, list) else predictions,
                   len(block))
            )

        filled = body.get("filled_features") or []
        if filled:
            raise EndpointError(
                "%s filled %d feature(s) with 0 rather than reading them from the "
                "request: %s. The request was built from the model's own feature "
                "list, so this means the deployed artifact expects features that "
                "list does not name -- train/serve skew, and the predictions "
                "would be wrong without any error."
                % (endpoint, len(filled), filled[:12])
            )

        tiers = body.get("risk_tiers") or []
        if want_tiers and len(tiers) != len(block):
            raise EndpointError(
                "%s returned %d risk tiers for %d rows" % (endpoint, len(tiers), len(block))
            )

        return {"predictions": predictions, "risk_tiers": tiers, "filled_features": []}

    def _predict(self, endpoint, matrix, want_tiers):
        """Every batch, concurrently, reassembled in row order."""
        from concurrent.futures import ThreadPoolExecutor

        columns = list(matrix.columns)
        # object dtype would mean a column the engineering step left as text, and
        # tolist() on it produces strings the endpoint's float() would reject.
        # Converting the whole matrix once is also what keeps the per-batch work
        # to a slice rather than a conversion.
        values = matrix.to_numpy(dtype=np.float64).tolist()

        spans = _batches(len(values), self.batch_rows)
        results = [None] * len(spans)
        started = time.time()

        def run(index):
            start, stop = spans[index]
            results[index] = self._call_block(endpoint, columns, values[start:stop], want_tiers)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            # list() rather than a bare map: it forces every future and re-raises
            # the first exception here, instead of leaving a failed batch as a
            # None that turns into a confusing error two lines down.
            list(pool.map(run, range(len(spans))))

        predictions, tiers = [], []
        for block in results:
            predictions.extend(block["predictions"])
            tiers.extend(block["risk_tiers"])

        logger.info(
            "scoring | %s | %d rows in %d batches, %.1fs"
            % (endpoint, len(values), len(spans), time.time() - started)
        )
        return np.asarray(predictions, dtype=np.float64), tiers

    def score(self, loans):
        """
        ExpectedLossTool.run's contract, computed against the endpoints.

        Every aggregate below is the same expression the local tool uses,
        including the total_exposure > 0 guards -- a query can legitimately
        retrieve loans whose outstanding balance is zero, and the exposure
        weighted averages are 0/0 for that population rather than an error.
        """
        if "exposure_at_default" not in loans.columns:
            raise ValueError("loans must include an 'exposure_at_default' column")

        X_pd = self._select(loans, self.pd_features, "PD")
        X_lgd = self._select(loans, self.lgd_features, "LGD")

        predicted_pd, risk_tiers = self._predict(self.pd_endpoint, X_pd, want_tiers=True)
        # LGD is already clipped to [0, 1] by inference.py, which is where the
        # local tool's .clip(0, 1) moved to. Not re-clipped here: a value outside
        # the range would mean the endpoint did not do it, and quietly clipping it
        # again would hide that.
        predicted_lgd, _ = self._predict(self.lgd_endpoint, X_lgd, want_tiers=False)

        out_of_range = int(((predicted_lgd < 0.0) | (predicted_lgd > 1.0)).sum())
        if out_of_range:
            raise EndpointError(
                "%d LGD prediction(s) fell outside [0, 1]. The endpoint clips, so "
                "this means the deployed inference code is not the version in "
                "Deploy/sagemaker/inference.py." % out_of_range
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
            # From the endpoint's tiers, not re-derived -- see the module
            # docstring. normalize=True to match ExpectedLossTool exactly: these
            # are shares of the loan count, not of exposure.
            risk_tier_distribution=pd.Series(risk_tiers).value_counts(normalize=True).to_dict(),
            scored_df=scored_df,
        )
