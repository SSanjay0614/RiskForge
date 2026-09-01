"""
Runs the repository's FeatureEngineeringTool, unchanged, and then checks the
two things a copied module cannot check for itself.

The tool is imported, not reimplemented -- see Deploy/fargate/stage.py. It
applies the transformations of 01_feature_engineering.ipynb, loads the
addr_state and emp_title frequency encodings from the training artifacts rather
than recomputing them from the batch, and drops rows it cannot engineer. All of
that is its business and none of it is repeated here.

What is added is two assertions about the join between the tool and its
artifacts, because that join is new in the container and silent when it breaks:

  * **Frequency-map coverage.** `df["emp_title"].map(self.emp_title_freq_map)`
    followed by `.fillna(0)` is correct behaviour for a job title the training
    data never saw. It is also exactly what a map loaded from the wrong file, or
    keyed differently than the column, looks like: every row NaN, every row
    filled with 0, no error, and a PD that is wrong by however much employment
    frequency was worth. So the fill rate is measured before the tool's fillna
    can hide it, and a rate below a floor fails the task.
  * **addr_state is 51 keys and a 2-letter code**, so an unmapped state is not a
    plausible new value -- it is a mismatch. Checked separately, at a tighter
    threshold, for that reason.

The point is not that either failure is likely. It is that both are invisible:
the run succeeds, the JSON is well formed, and the number is wrong.
"""
import joblib

from tools.feature_engineering_tool import FeatureEngineeringTool

from config import ADDR_STATE_FREQ_MAP_PATH, EMP_TITLE_FREQ_MAP_PATH

from utils.logger import logger

# 378,168 titles were in the training map, and Lending Club's emp_title is free
# text a borrower typed, so a real batch always misses some: a 15,976-row slice
# of California 60-month 2018 loans measured 0.82, and it will differ by vintage
# and by state.
#
# The floor is therefore set at 0.50 rather than just under the observed rate. The
# failure being caught is a rate near ZERO -- the wrong artifact, or a column
# keyed differently than the map -- and a floor tuned tight to one slice would
# fail honest queries while catching nothing extra. Coverage is reported in the
# output either way, so a rate that drifts is visible without being fatal.
MIN_EMP_TITLE_COVERAGE = 0.50

# 51 keys against a 2-letter state code. Anything under this is a broken join.
MIN_ADDR_STATE_COVERAGE = 0.999


def _coverage(series, mapping):
    """Share of non-null values in `series` that `mapping` has a key for."""
    present = series.dropna()
    if len(present) == 0:
        return None
    return float(present.isin(set(mapping.keys())).mean())


def check_coverage(raw_df):
    """
    Measured on the raw frame, before the tool runs, so the tool's fillna(0)
    cannot mask the result. Returns the two rates for the response.
    """
    rates = {}

    if "addr_state" in raw_df.columns:
        addr_map = joblib.load(ADDR_STATE_FREQ_MAP_PATH)
        rate = _coverage(raw_df["addr_state"], addr_map)
        rates["addr_state_coverage"] = rate
        if rate is not None and rate < MIN_ADDR_STATE_COVERAGE:
            raise ValueError(
                "addr_state frequency map covers only %.4f of rows (floor %.4f, "
                "%d keys in the map). A 2-letter state code that the training map "
                "does not have is a mismatched artifact, not a new state."
                % (rate, MIN_ADDR_STATE_COVERAGE, len(addr_map))
            )

    if "emp_title" in raw_df.columns:
        emp_map = joblib.load(EMP_TITLE_FREQ_MAP_PATH)
        rate = _coverage(raw_df["emp_title"], emp_map)
        rates["emp_title_coverage"] = rate
        if rate is not None and rate < MIN_EMP_TITLE_COVERAGE:
            raise ValueError(
                "emp_title frequency map covers only %.4f of rows (floor %.4f, "
                "%d keys in the map). FeatureEngineeringTool fills the misses "
                "with 0, so this would have produced a wrong PD for most of the "
                "portfolio without raising anything."
                % (rate, MIN_EMP_TITLE_COVERAGE, len(emp_map))
            )

    return rates


def engineer(raw_df, tool=None):
    """
    (engineered_df, diagnostics). `diagnostics` is the row accounting the
    interface needs to explain why fewer loans were scored than retrieved --
    carried across from CreditRiskAgent, which puts the same three fields inside
    credit_metrics for the same reason.
    """
    rates = check_coverage(raw_df)

    tool = tool or FeatureEngineeringTool()
    result = tool.run(raw_df)

    logger.info(
        "features | input=%d output=%d dropped=%d reasons=%s"
        % (result.input_row_count, result.output_row_count,
           result.rows_dropped, result.dropped_reason_counts)
    )

    engineered = result.engineered_df.copy()

    # Same alias, applied again: FeatureEngineeringTool keeps
    # outstanding_balance and the scoring step needs the exposure name. Carried
    # across from CreditRiskAgent, including the condition.
    if ("exposure_at_default" not in engineered.columns
            and "outstanding_balance" in engineered.columns):
        engineered["exposure_at_default"] = engineered["outstanding_balance"]

    diagnostics = {
        "rows_retrieved": result.input_row_count,
        "rows_engineered": result.output_row_count,
        "rows_dropped": result.rows_dropped,
        "dropped_reason_counts": result.dropped_reason_counts,
    }
    diagnostics.update(rates)

    return engineered, diagnostics
