import pandas as pd
import numpy as np

from tools.concentration_tool import ConcentrationTool


tool = ConcentrationTool()

# Synthetic sample loans across sectors -- swap in a real portfolio slice
# once the Data Agent is wired up. Purpose skewed to show a concentrated case.
rng = np.random.default_rng(42)
purposes = rng.choice(
    ["debt_consolidation", "credit_card", "home_improvement", "small_business", "other"],
    size=300,
    p=[0.55, 0.2, 0.1, 0.1, 0.05],
)
loans = pd.DataFrame({
    "exposure_at_default": rng.uniform(2000, 25000, size=300),
    "purpose": purposes,
})

result = tool.run(loans, segment_column="purpose")

print("-" * 60)
print(f"Segment column:          {result.segment_column}")
print(f"HHI (0-1 scale):         {result.hhi_score:.4f}")
print(f"HHI (0-10000 scale):     {result.hhi_score_10000_scale:.1f}")
print(f"Diversification level:   {result.diversification_level}")
print("Segment shares:")
for segment, share in sorted(result.segment_shares.items(), key=lambda x: -x[1]):
    print(f"  {segment:>20}: {share:.4f}")
