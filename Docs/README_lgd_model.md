# Loss Given Default (LGD) Model

A regression model predicting the fraction of exposure lost on a defaulted loan, built on Lending Club's public loan dataset. Feeds Expected Loss alongside the PD model: `EL = PD × LGD × EAD`.

## Dataset & Scope

- **Source:** [Lending Club accepted loans dataset](https://www.kaggle.com/datasets/wordsforthewise/lending-club) (2007–2018Q4)
- **Population:** Charged Off loans only (~268K) — LGD is only defined for loans that have actually defaulted, making this a different population from the PD model's training set (which includes both Fully Paid and Charged Off loans).

## Target Definition

```
recovery_rate = recoveries / EAD_at_default
LGD = 1 - recovery_rate
```

`EAD_at_default` — the exposure still outstanding when the loan defaulted — is approximated as `funded_amnt - total_rec_prncp` (original loan amount minus principal already repaid via normal payments before charge-off). This matters: dividing by the original loan size instead of the outstanding balance at default systematically overstates loss for any loan that had meaningfully paid down before defaulting. `out_prncp` cannot be used directly for this, since it is zeroed out once a loan is charged off.

## Features

Mostly reuses the PD model's origination feature set (loan/borrower characteristics at issuance). Two notable additions specific to LGD:

- **`prncp_repaid_ratio`** — principal repaid via normal payments, as a fraction of the original loan. A distinct concept from `recoveries` (post-default collections activity), so not circular with the target.
- **`debt_settlement_flag`** — excluded from the PD model as leakage (entering settlement is a consequence of default, which would leak the outcome being predicted there). Included here: since default is already the entire population being modeled, whether a borrower settled is a genuine predictor of *how much* gets recovered, not a leak of *whether* default happened.

`collection_recovery_fee`, `out_prncp`, and `total_pymnt` remain excluded — these overlap or are co-computed with the target itself.

## Modeling

Three candidates were compared: Linear Regression (baseline), a logit-link regression (approximating Beta regression by transforming the bounded [0,1] target before fitting), and XGBoost. **XGBoost** was selected.

### Test set performance

| Metric | Value |
|---|---|
| MAE | 0.082 |
| RMSE | 0.116 |
| R² | 0.235 |

LGD is a structurally difficult regression target — recovery outcomes depend heavily on factors barely observable in loan-level data (collections effort, debt sale terms, borrower circumstances post-default). Published LGD benchmarking studies report R² in the range of roughly 4–43% across techniques and datasets; 0.235 sits comfortably within that range.

The logit-link regression underperformed the plain baseline (negative R²). The LGD distribution has a large point mass near 1.0 (many charged-off loans recover almost nothing), which the logit transform maps to extreme values, distorting a linear fit. XGBoost's tree splits require no distributional assumption about the target and handle this naturally.

## Calibration Check

Predictions were bucketed into deciles and compared against actual mean LGD per bucket, as a regression-appropriate substitute for a classification calibration curve. Predicted and actual means track closely across all deciles (e.g., 0.900 predicted vs. 0.899 actual; 0.940 vs. 0.947 at the top decile), with no systematic bias at any LGD level.

## Artifacts

| File | Description |
|---|---|
| `lgd_model.joblib` | Trained XGBoost LGD model |
| `lgd_model_feature_names.joblib` | Ordered feature list expected at inference |
| `lgd_df_processed.parquet` | Processed training dataset |

## Notebook

- `03_lgd_model.ipynb` — data loading, target construction, feature engineering, model training, comparison, calibration check
