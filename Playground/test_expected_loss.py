import pandas as pd

from tools.expected_loss_tool import ExpectedLossTool

from config import BEHAVIORAL_PD_PARQUET

tool = ExpectedLossTool()

# Reuse a slice of the processed behavioral dataset as a stand-in portfolio.
# exposure_at_default isn't part of that file, so a placeholder is added here
# for testing -- swap in the real demo_portfolio_scored.parquet EAD column
# once the Data Agent is wired up.
loans = pd.read_parquet(BEHAVIORAL_PD_PARQUET).sample(50, random_state=42)
loans["exposure_at_default"] = loans["loan_amnt"] if "loan_amnt" in loans.columns else 10000

result = tool.run(loans)

print("-" * 60)
print(f"Loans scored:              {result.loan_count}")
print(f"Total exposure:            {result.total_exposure:,.2f}")
print(f"Total expected loss:       {result.total_expected_loss:,.2f}")
print(f"Expected loss rate:        {result.expected_loss_rate:.4f}")
print(f"Exposure-weighted avg PD:  {result.exposure_weighted_avg_pd:.4f}")
print(f"Exposure-weighted avg LGD: {result.exposure_weighted_avg_lgd:.4f}")
print(f"Risk tier distribution:    {result.risk_tier_distribution}")
