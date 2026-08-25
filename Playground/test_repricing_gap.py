import pandas as pd
import numpy as np

from tools.repricing_gap_tool import RepricingGapTool


tool = RepricingGapTool(as_of_date=pd.Timestamp("2018-12-01"))

# Synthetic sample loans -- raw fields (issue_date + term), not a
# precomputed months_remaining column, since that's what a real loans table
# would actually store. months_remaining is derived internally by the tool.
rng = np.random.default_rng(42)
issue_dates = pd.to_datetime("2018-12-01") - pd.to_timedelta(
    rng.integers(0, 48, size=200) * 30, unit="D"
)
loans = pd.DataFrame({
    "exposure_at_default": rng.uniform(2000, 25000, size=200),
    "issue_date": issue_dates,
    "term": rng.integers(0, 2, size=200),  # 0 = 36mo, 1 = 60mo
})

result = tool.run(loans)

print("-" * 60)
for bucket in result.buckets:
    print(
        f"{bucket.bucket_label:>8} | "
        f"RSA: {bucket.rate_sensitive_assets:>12,.2f} | "
        f"RSL: {bucket.rate_sensitive_liabilities:>12,.2f} | "
        f"Gap: {bucket.gap:>12,.2f}"
    )

print("-" * 60)
print(f"Total RSA: {result.total_rate_sensitive_assets:,.2f}")
print(f"Total RSL: {result.total_rate_sensitive_liabilities:,.2f}")
print(f"Net gap:   {result.net_gap:,.2f}")
print(f"Liabilities synthetic: {result.liabilities_are_synthetic}")
