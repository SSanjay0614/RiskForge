# Behavioral PD Model

A probability-of-default (PD) model for **existing loans**, built on Lending Club's public loan dataset. Unlike a standard application-scoring model (which predicts default risk for *new* applicants), this model is trained to assess risk on loans already on the book — feeding portfolio-level credit risk analysis rather than loan origination decisions.

## Dataset

- **Source:** [Lending Club accepted loans dataset](https://www.kaggle.com/datasets/wordsforthewise/lending-club) (2007–2018Q4)
- **Training population:** ~1.27M resolved loans (Fully Paid / Charged Off / Default)

## Approach

**Origination features** — borrower profile and credit bureau data at time of application (income, DTI, FICO, credit history, loan terms).

**Behavioral features** — signals from the loan's life after origination: `on_payment_plan`, `entered_hardship`, `distress_combo`.

A broader set of post-origination fields (updated FICO pulls, last credit-pull date, and derivatives) were evaluated and excluded after a leakage investigation showed their timing was anchored to loan resolution rather than preceding it. The remaining behavioral features are modest but leakage-free.

## Modeling

Five candidate models were trained and compared: Logistic Regression, Random Forest, XGBoost (Optuna-tuned), LightGBM, CatBoost, plus a stacking ensemble. **XGBoost** was selected as the final model, using a combined discrimination + calibration criterion (80% ROC-AUC weight, 20% Brier score weight) rather than raw AUC alone, prioritized for downstream explainability.

### Test set performance (calibrated)

| Metric | Value |
|---|---|
| ROC-AUC | 0.729 |
| Gini coefficient | 0.458 |
| KS statistic | 0.333 |
| Brier score | 0.143 |
| Log loss | 0.448 |

Results are consistent with published benchmarks on this dataset (typically 0.65–0.75 AUC). An earlier version of this model reached 0.96 AUC due to a data leakage issue that was subsequently identified and fixed.

## Calibration

Raw XGBoost output is not a reliable probability — it ranks well but is systematically overconfident at the extremes. The final model applies **isotonic calibration** (`CalibratedClassifierCV`, fit on the validation set) so that a predicted PD of 0.20 corresponds to an actual ~20% observed default rate among similarly-scored loans. This matters directly for downstream use, since Expected Loss (`EL = PD × LGD × EAD`) is only meaningful if PD is calibrated, not just well-ranked.

## Risk Tiers

Calibrated PD is bucketed into four tiers for portfolio-level reporting:

| Tier | PD Range |
|---|---|
| Low | < 5% |
| Medium | 5–15% |
| High | 15–30% |
| Very High | ≥ 30% |

Tiers support portfolio/segment-level statements (e.g., "18% of retail segment exposure sits in the High or Very High tier") rather than individual accept/reject decisions, which are out of scope for this model.

## Explainability

Global SHAP feature importance is used to identify portfolio-wide risk drivers (e.g., `sub_grade`, `term`, `fico_score_origination`). Per-loan local explanations are intentionally not produced, since this model supports aggregate risk reporting, not individual lending decisions.

## Artifacts

| File | Description |
|---|---|
| `pd_model_calibrated.joblib` | Trained, calibrated XGBoost model |
| `pd_model_feature_names.joblib` | Ordered feature list expected at inference |
| `pd_risk_tier_cutoffs.joblib` | Risk tier PD boundaries |

## Notebooks

- `01_feature_engineering.ipynb` — data loading, label engineering, feature construction
- `02_modeling_evaluation.ipynb` — model training, comparison, calibration, evaluation
