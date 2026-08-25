"""
Tests RegulatoryCapitalTool against REAL loans -- pulled from credit_risk.db,
feature-engineered, and scored with the real trained PD and LGD models. No
LLM involved (this tool makes no LLM calls).
"""

import joblib
import pandas as pd

from tools.sql_executor_tool import SQLExecutorTool
from tools.feature_engineering_tool import FeatureEngineeringTool
from tools.regulatory_capital_tool import RegulatoryCapitalTool

from config import PD_MODEL_PATH, PD_FEATURE_NAMES_PATH, LGD_MODEL_PATH, LGD_FEATURE_NAMES_PATH


executor = SQLExecutorTool()
fe_tool = FeatureEngineeringTool()
capital_tool = RegulatoryCapitalTool()

pd_model = joblib.load(PD_MODEL_PATH)
pd_feature_names = joblib.load(PD_FEATURE_NAMES_PATH)
lgd_model = joblib.load(LGD_MODEL_PATH)
lgd_feature_names = joblib.load(LGD_FEATURE_NAMES_PATH)


sql_query = """
SELECT *
FROM Loans
JOIN Borrowers USING(loan_id)
LIMIT 2000
"""

exec_result = executor.run(sql_query)
print(f"Pulled {exec_result.row_count} real rows from credit_risk.db")

rows_df = exec_result.rows_df.copy()
rows_df["exposure_at_default"] = rows_df["outstanding_balance"]

fe_result = fe_tool.run(rows_df)
print(f"Feature engineered: {fe_result.output_row_count} rows survived ({fe_result.rows_dropped} dropped)")

engineered = fe_result.engineered_df.copy()
if "exposure_at_default" not in engineered.columns and "outstanding_balance" in engineered.columns:
    engineered["exposure_at_default"] = engineered["outstanding_balance"]


def align(df, feature_names):
    missing = [f for f in feature_names if f not in df.columns]
    for f in missing:
        df[f] = 0
    return df[feature_names]


X_pd = align(engineered.copy(), pd_feature_names)
X_lgd = align(engineered.copy(), lgd_feature_names)

engineered["predicted_pd"] = pd_model.predict_proba(X_pd)[:, 1]
engineered["predicted_lgd"] = lgd_model.predict(X_lgd).clip(0, 1)

print(f"Predicted PD range: {engineered['predicted_pd'].min():.4f} - {engineered['predicted_pd'].max():.4f}")
print(f"Predicted LGD range: {engineered['predicted_lgd'].min():.4f} - {engineered['predicted_lgd'].max():.4f}")

capital_result = capital_tool.run(engineered)

print()
print("-" * 60)
print(f"Loan count:                        {capital_result.loan_count}")
print(f"Total EAD:                         {capital_result.total_ead:,.2f}")
print(f"Total RWA:                         {capital_result.total_rwa:,.2f}")
print(f"Total capital requirement (8%):    {capital_result.total_capital_requirement_8pct:,.2f}")
print(f"Exposure-weighted avg correlation: {capital_result.exposure_weighted_avg_correlation:.4f}")
print(f"Exposure-weighted avg K:           {capital_result.exposure_weighted_avg_k:.4f}")
print(f"Average risk weight:               {capital_result.avg_risk_weight_pct:.1f}%")
