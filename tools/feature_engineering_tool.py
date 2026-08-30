import joblib
import pandas as pd
import numpy as np

from tools.base_tool import BaseTool

from dmodels.feature_engineering_result import FeatureEngineeringResult

from config import ADDR_STATE_FREQ_MAP_PATH, EMP_TITLE_FREQ_MAP_PATH


REQUIRED_RAW_COLUMNS = [
    "loan_amnt", "term", "int_rate", "installment", "sub_grade", "purpose",
    "issue_date", "outstanding_balance", "on_payment_plan", "entered_hardship",
    "annual_inc", "emp_length", "emp_title", "home_ownership", "verification_status",
    "addr_state", "dti", "fico_range_low", "fico_range_high", "earliest_cr_line",
    "open_acc", "total_acc", "revol_bal", "revol_util", "delinq_2yrs",
    "acc_now_delinq", "inq_last_6mths", "mths_since_last_delinq",
    "mths_since_last_record", "num_tl_90g_dpd_24m", "tot_coll_amt", "tot_cur_bal",
    "mo_sin_old_rev_tl_op", "pct_tl_nvr_dlq", "pub_rec", "mort_acc",
    "pub_rec_bankruptcies",
]

EMP_LENGTH_MAP = {
    "< 1 year": 0, "1 year": 1, "2 years": 2, "3 years": 3, "4 years": 4,
    "5 years": 5, "6 years": 6, "7 years": 7, "8 years": 8, "9 years": 9,
    "10+ years": 10,
}

TERM_MAP = {36: 0, 60: 1}  # SQLite stores term as raw months, not the CSV's string form

# Categorical levels present in the training data, pinned here for the same
# reason the frequency maps are loaded from artifacts rather than recomputed:
# get_dummies on a runtime subset emits only the levels that subset happens to
# contain, so the encoding would otherwise depend on batch composition.
# home_ownership's ANY/NONE are folded into OTHER before encoding.
TRAINING_CATEGORY_LEVELS = {
    "verification_status": ["Not Verified", "Source Verified", "Verified"],
    "home_ownership": ["MORTGAGE", "OTHER", "OWN", "RENT"],
    "purpose": [
        "car", "credit_card", "debt_consolidation", "educational",
        "home_improvement", "house", "major_purchase", "medical", "moving",
        "other", "renewable_energy", "small_business", "vacation", "wedding",
    ],
}

EXPECTED_DUMMY_COLUMNS = [
    f"{col}_{level}"
    for col, levels in TRAINING_CATEGORY_LEVELS.items()
    for level in levels
]

# Training used drop_first=True, which drops the alphabetically first level of
# each column. Those become the reference category and are absent from the
# saved model feature lists, so they are dropped here too -- explicitly, by
# name, instead of positionally.
DUMMY_BASELINE_COLUMNS = [
    f"{col}_{levels[0]}" for col, levels in TRAINING_CATEGORY_LEVELS.items()
]

SUBGRADE_ORDER = [f"{g}{s}" for g in ["A", "B", "C", "D", "E", "F", "G"] for s in range(1, 6)]
SUBGRADE_FLOAT_MAP = {sg: float(f"{i // 5 + 1}.{i % 5 + 1}") for i, sg in enumerate(SUBGRADE_ORDER)}

CLUSTER_COLS = [
    "pct_tl_nvr_dlq", "mo_sin_old_rev_tl_op", "tot_coll_amt_log",
    "tot_cur_bal_log", "credit_card_util_pct", "num_tl_90g_dpd_24m",
]


class FeatureEngineeringTool(BaseTool):
    """
    Applies the same transformations as 01_feature_engineering.ipynb to a raw
    loan/borrower subset (e.g. returned by the Data Agent's SQL query),
    producing the engineered feature set the trained PD/LGD models expect.

    addr_state and emp_title frequency encodings are loaded from artifacts
    saved during training, NOT recomputed from the input batch -- recomputing
    them from a small runtime subset would produce different values than what
    the models were trained on (train/serve skew), silently corrupting
    predictions without raising any error.
    """

    def __init__(
        self,
        addr_freq_map_path: str = ADDR_STATE_FREQ_MAP_PATH,
        emp_title_freq_map_path: str = EMP_TITLE_FREQ_MAP_PATH,
    ):
        super().__init__("Feature Engineering Tool")

        self.addr_freq_map = joblib.load(addr_freq_map_path)
        self.emp_title_freq_map = joblib.load(emp_title_freq_map_path)

    def _validate_input(self, df: pd.DataFrame) -> None:

        missing = [c for c in REQUIRED_RAW_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"Input is missing required raw columns: {missing}")

    def run(self, raw_df: pd.DataFrame) -> FeatureEngineeringResult:

        self._validate_input(raw_df)

        df = raw_df.copy()
        input_row_count = len(df)
        dropped_reasons: dict = {}

        df["home_ownership"] = df["home_ownership"].replace(
            {"ANY": "OTHER", "NONE": "OTHER", "OTHER": "OTHER"}
        )

        # on_payment_plan / entered_hardship arrive pre-derived from the schema
        df["distress_combo"] = df["on_payment_plan"] + df["entered_hardship"]

        df["fico_score_origination"] = (df["fico_range_low"] + df["fico_range_high"]) / 2
        df.drop(columns=["fico_range_low", "fico_range_high"], inplace=True)

        df["credit_card_util_pct"] = df["revol_bal"] / (df["tot_cur_bal"] + 1)
        df["installment_income_ratio"] = df["installment"] / (df["annual_inc"] + 1)
        df["loan_amount_income_ratio"] = df["loan_amnt"] / (df["annual_inc"] + 1)

        # LGD-only features. Both sit on the PD leakage exclusion list in
        # 01_feature_engineering.ipynb and are re-included for LGD alone,
        # following 03_lgd_model.ipynb: the LGD population is already entirely
        # defaulted loans, so these speak to how much is lost, not to whether
        # default occurs. Neither name appears in the PD model's feature list,
        # and ExpectedLossTool selects each model's features strictly by name,
        # so the PD model cannot see either column.
        #
        # Training used total_rec_prncp, which this schema doesn't store, but
        # it is exactly recoverable from what it does store: loan_amnt ==
        # funded_amnt and funded_amnt - out_prncp == total_rec_prncp hold for
        # every row of the source data.
        df["prncp_repaid_ratio"] = (
            df["loan_amnt"] - df["outstanding_balance"]
        ) / (df["loan_amnt"] + 1)

        # Also absent from this schema, but 0 is the correct value here rather
        # than a fallback: entering settlement is a consequence of default, and
        # only 6 of 169,940 non-defaulted loans in the source data carry the
        # flag (0.004%).
        df["debt_settlement_flag"] = 0

        df["delinq_flag"] = (df["mths_since_last_delinq"] < 12).astype(int)
        df["bankruptcy_flag"] = (df["pub_rec_bankruptcies"] > 0).astype(int)
        df["historical_delinquency_rate"] = df["delinq_2yrs"] / (df["total_acc"] + 1)

        # drop_first=False so the emitted columns don't depend on which levels
        # this batch contains; the fixed training baseline is removed below.
        df = pd.get_dummies(
            df,
            columns=["verification_status", "home_ownership", "purpose"],
            drop_first=False,
        )

        for col in EXPECTED_DUMMY_COLUMNS:
            if col not in df.columns:
                df[col] = False  # level absent from this batch

        df.drop(columns=DUMMY_BASELINE_COLUMNS, inplace=True)

        df["emp_length"] = df["emp_length"].map(EMP_LENGTH_MAP)
        df["term"] = df["term"].map(TERM_MAP)

        before = len(df)
        df = df[df["dti"] > 0]
        dropped_reasons["non_positive_dti"] = before - len(df)

        # Frequency maps loaded from training artifacts -- see class docstring.
        df["addr_state_freq"] = df["addr_state"].map(self.addr_freq_map)
        df.drop(columns=["addr_state"], inplace=True)

        df["sub_grade"] = df["sub_grade"].map(SUBGRADE_FLOAT_MAP)
        if "grade" in df.columns:
            df.drop(columns=["grade"], inplace=True)  # redundant with sub_grade

        df["revol_util"] = df["revol_util"].clip(upper=100)
        df["fico_dti_ratio"] = df["fico_score_origination"] / (1 + df["dti"])

        df["annual_inc_log"] = np.log1p(df["annual_inc"])
        df["dti_log"] = np.log1p(df["dti"])
        df["revol_bal_log"] = np.log1p(df["revol_bal"])
        df["tot_coll_amt_log"] = np.log1p(df["tot_coll_amt"])
        df["tot_cur_bal_log"] = np.log1p(df["tot_cur_bal"])
        # dti is deliberately NOT dropped: the PD model uses dti_log, but the
        # LGD model was trained on raw dti (see 03_lgd_model.ipynb), and each
        # model's feature list is applied by name downstream.
        df.drop(columns=["annual_inc", "revol_bal", "tot_coll_amt", "tot_cur_bal"], inplace=True)

        # SQLite stores dates as ISO text, unlike the CSV's 'Mon-YYYY' format --
        # standard parsing, no explicit format string needed.
        df["earliest_cr_line"] = pd.to_datetime(df["earliest_cr_line"], errors="coerce")
        issue_date_parsed = pd.to_datetime(df["issue_date"], errors="coerce")
        df["account_age_years"] = (issue_date_parsed - df["earliest_cr_line"]).dt.days / 365.25
        df.drop(columns=["earliest_cr_line"], inplace=True)

        df["emp_title_freq"] = df["emp_title"].map(self.emp_title_freq_map)
        df.drop(columns=["emp_title"], inplace=True)

        for col in ["inq_last_6mths", "mths_since_last_delinq", "mths_since_last_record",
                    "revol_util", "num_tl_90g_dpd_24m", "emp_length"]:
            df[f"{col}_missing"] = df[col].isna().astype(int)

        df["mths_since_last_delinq"] = df["mths_since_last_delinq"].fillna(999)
        df["mths_since_last_record"] = df["mths_since_last_record"].fillna(999)
        df["revol_util"] = df["revol_util"].fillna(0)
        df["emp_title_freq"] = df["emp_title_freq"].fillna(0)
        df["emp_length"] = df["emp_length"].fillna(0)

        # PD interaction features, created in 02_modeling_evaluation.ipynb after
        # 01's output was loaded -- so they belong here, downstream of the
        # imputations above, which is the order the model was trained on.
        df["revolutil_fico"] = df["revol_util"] * df["fico_score_origination"]
        df["dti_loaninc_ratio"] = df["dti_log"] * df["loan_amount_income_ratio"]
        df["accage_openacc"] = df["account_age_years"] * df["open_acc"]
        df["emplen_verif"] = df["emp_length"] * df["verification_status_Verified"]
        df["inst_inc_purpose_debtcon"] = (
            df["installment_income_ratio"] * df["purpose_debt_consolidation"]
        )

        before = len(df)
        df = df.dropna(subset=CLUSTER_COLS)
        dropped_reasons["missing_credit_report_cluster"] = before - len(df)

        before = len(df)
        df = df.dropna(subset=["inq_last_6mths"])
        dropped_reasons["missing_inq_last_6mths"] = before - len(df)

        if df.isna().sum().sum() > 0:
            raise ValueError("Unhandled NaNs remain after feature engineering")
        if np.isinf(df.select_dtypes(include=[np.number])).any().any():
            raise ValueError("Inf values remain after feature engineering")

        return FeatureEngineeringResult(
            engineered_df=df,
            input_row_count=input_row_count,
            output_row_count=len(df),
            rows_dropped=input_row_count - len(df),
            dropped_reason_counts=dropped_reasons,
        )
