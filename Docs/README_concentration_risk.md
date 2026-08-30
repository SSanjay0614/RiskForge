# Concentration Risk — the Herfindahl-Hirschman Index (HHI)

How RiskForge measures whether the portfolio's exposure is spread out or piled into a few segments. One formula, no model, no assumptions — everything comes from the retrieved rows.

Implemented in `tools/concentration_tool.py`. Rendered by `render_concentration()` in `Frontend/views/risk_analysis.py` and section 4 of the PDF in `Frontend/memo.py`.

---

## 1. The question being answered

Two portfolios can have identical expected loss and be very differently dangerous. A book spread evenly across twelve loan purposes and fifty states survives a shock to any one of them. A book where 58% of the money sits in one purpose does not — one bad sector, and a large share of the portfolio moves together.

Expected loss cannot see this, because it averages. Concentration risk is the measure that does see it.

## 2. The formula

Take each segment's **share of total exposure**, square it, and add the squares up:

```
share(i) = exposure in segment i / total exposure
HHI      = Σ share(i)²                       # 0 to 1
HHI_10000 = HHI × 10000                      # the reported scale, 0 to 10000
```

### Why square the shares?

Squaring is what makes the index notice big segments rather than counting small ones. Shares always sum to 1, so a plain sum tells you nothing. Squaring makes a 0.58 share contribute 0.336 while a 0.02 share contributes 0.0004 — over 800 times less. Concentration is driven almost entirely by the largest few segments, and squaring reproduces exactly that.

### Reading the raw value

Two reference points make the number intuitive:

- **All exposure in one segment:** HHI = 1² = 1 → **10,000**. The maximum.
- **Split evenly across n segments:** HHI = n × (1/n)² = 1/n → **10,000 / n**. Twelve equal purposes would give 833.

And the reciprocal is genuinely useful:

```
effective number of segments = 1 / HHI
```

An HHI of 0.402 means the book behaves as if it were spread across **2.5 equal segments**, however many segments it nominally has.

---

## 3. Worked example — 113,008 California loans, by purpose

Exposure shares, largest first (12 purposes in total):

| Purpose | Exposure share | Share² |
|---|---|---|
| debt_consolidation | 57.885% | 0.335070 |
| credit_card | 24.478% | 0.059915 |
| home_improvement | 5.811% | 0.003377 |
| other | 5.364% | 0.002877 |
| major_purchase | 2.024% | 0.000410 |
| small_business | 1.416% | 0.000200 |
| *remaining 6 purposes* | 3.022% | 0.000191 |
| **Total** | **100%** | **0.402040** |

```
HHI       = 0.402040
HHI_10000 = 4,020
effective segments = 1 / 0.402040 = 2.49
```

**4,020 → "Highly Concentrated."** Two purposes hold 82% of the money, and debt consolidation alone contributes 83% of the entire index (0.335 of 0.402). The other ten purposes together contribute less than 2% of it. That is what the index is telling you, and it is a real property of consumer lending platforms rather than a quirk of this query.

## 4. The thresholds

| HHI (0–10,000) | Label |
|---|---|
| < 1,500 | Diversified |
| 1,500 – 2,500 | Moderately Concentrated |
| > 2,500 | Highly Concentrated |

These are the **US DOJ / FTC merger-guideline bands**, borrowed here and applied to portfolio segments instead of market shares. It is the same underlying metric — HHI was designed for market concentration — but the bands were calibrated for antitrust review, not for credit portfolios.

That is an honest adaptation, not a citation. The `max_hhi_10000_scale` limit of 2,500 in `Risk_Limits` is labelled **internal policy** for exactly this reason: Basel's credit risk framework (CRE30–32) sets no portfolio-level sector concentration limit. Basel addresses single-counterparty concentration under the Large Exposures framework, which does not apply to sector-level retail exposure, and leaves portfolio concentration to each bank's own risk appetite framework.

---

## 5. Two dimensions, always both

The query itself rarely says which segmentation is wanted, so both standard ones are computed on every run (`agents/interest_rate_concentration_agent.py`):

| Dimension | Column | Meaning |
|---|---|---|
| Sector | `purpose` | What the loan is for — debt consolidation, credit card, home improvement, … |
| Region | `addr_state` | Borrower's US state |

They answer different questions. Sector concentration asks "what happens if one kind of borrowing goes bad?" Region concentration asks "what happens if one local economy goes bad?" A book can be fine on one and badly exposed on the other.

Each is computed independently and defensively: if a query's SQL did not return `purpose` or `addr_state`, that one metric is skipped with a logged reason rather than failing the whole agent.

## 6. Exposure-weighted, not loan-counted

Shares are computed from `exposure_at_default` (the current outstanding balance), never from the number of loans. Ten thousand $1,000 loans and two hundred $50,000 loans are the same $10m of risk, and it is the money that is at risk, not the row count. Every other risk figure in RiskForge is exposure-weighted for the same reason, so the numbers stay comparable.

## 7. Reading the chart

A horizontal bar per segment, longest at the value it holds, with the headline in the chart title: `By purpose | HHI 4,020 | Highly Concentrated`. The two charts sit side by side under **Portfolio concentration**, purpose on the left and region on the right.

Horizontal bars rather than a pie chart on purpose — segment names like `debt_consolidation` are long, and comparing bar lengths is easier than comparing pie slice angles. The chart height grows with the number of segments so labels never overlap.

What to look at: the length of the **first** bar, and how fast the bars shrink after it. A long first bar with a steep drop-off is a concentrated book, and that is precisely what the HHI number is summarising.

---

## 8. The filtered-query artefact — read this before trusting a region HHI

HHI is computed on **the rows the query returned**, not on the whole portfolio. So a query that filters to one segment makes that dimension's HHI 10,000 by construction:

> "What is our expected loss and concentration risk for California loans?"
> → every row has `addr_state = 'CA'` → one segment → share 1.0 → **region HHI = 10,000, "Highly Concentrated"**

That is arithmetically correct and analytically meaningless. It says "all the California loans are in California."

This matters for the compliance check, because `max_hhi_10000_scale` is evaluated against the **larger** of the two dimensions:

```python
max(concentration_by_purpose.hhi_score_10000_scale,
    concentration_by_region.hhi_score_10000_scale)
```

So any single-state query breaches the 2,500 concentration limit automatically, on geography, regardless of what the sector mix looks like. When you see a concentration breach on a filtered query, check which dimension drove it before reading anything into it. Sector HHI on the same rows (4,020 in the example) is the meaningful figure there — it was not forced by the filter.

## 9. What this deliberately does not do

| Not measured | Why it matters |
|---|---|
| Single-name concentration | This is retail: ~113k borrowers, no one of them large enough to matter. In a corporate book, the largest exposures would be the whole question. |
| Correlation between segments | HHI treats segments as independent labels. Credit card and debt consolidation borrowers are in fact very similar, so the true diversification is worse than 2.5 effective segments suggests. |
| Sub-segment structure | `purpose` is Lending Club's own taxonomy — 12 coarse buckets. A finer one would give a different HHI on the same money. |
| Vintage or grade concentration | Only sector and region are computed. Concentration in one issue year or one credit grade is not flagged. |
| Absolute size | HHI is scale-free. A $1m book and a $1bn book with the same shares get the same score. |

## 10. Where it lives

| Item | Location |
|---|---|
| Formula and thresholds | `tools/concentration_tool.py` |
| Threshold constants | `UNCONCENTRATED_THRESHOLD = 1500`, `MODERATE_THRESHOLD = 2500` |
| Result fields (`hhi_score`, `hhi_score_10000_scale`, `diversification_level`, `segment_shares`) | `dmodels/concentration_result.py` |
| Called by | `agents/interest_rate_concentration_agent.py`, once per dimension |
| Compliance check | `agents/compliance_agent.py`, `max_hhi_10000_scale`, max of the two dimensions |
| Limit and its source label | `Risk_Limits` table, `internal` |
| On-screen rendering | `render_concentration()`, `Frontend/views/risk_analysis.py` |
| PDF section 4 | `Frontend/memo.py` |

Related: [Interest rate risk](README_interest_rate_risk.md), [PD model](README_behavioral_pd_model.md), [LGD model](README_lgd_model.md).
