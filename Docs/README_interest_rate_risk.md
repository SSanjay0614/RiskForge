# Interest Rate Risk — Repricing Gap and Earnings at Risk

How RiskForge measures what a change in interest rates would do to this loan book. No model is involved: every number here comes from arithmetic on the retrieved rows plus three clearly-labelled assumptions about the deposit side.

Implemented in `tools/repricing_gap_tool.py`. Rendered by `render_interest_rate()` in `Frontend/views/risk_analysis.py` and section 5 of the PDF in `Frontend/memo.py`.

---

## 1. The question being answered

A bank makes money on the spread between what it charges borrowers and what it pays depositors. The two sides do not move together. Deposits reprice almost immediately — a savings account rate can be changed next month — while a 60-month fixed-rate loan keeps paying its original rate until it matures.

So if rates rise, a bank whose funding reprices faster than its lending pays more right away and only earns more later. That timing mismatch is interest rate risk, and the standard first measure of it is the **repricing gap**.

The gap answers "which side reprices first, and by how much". The **earnings-at-risk** figure turns that into money: how much of the next twelve months of profit a rate move would take away.

---

## 2. What is observed and what is assumed

This matters more here than anywhere else in RiskForge, because this portfolio is a **lending book, not a bank**. It has loans; it has no deposits, because Lending Club was a lending platform.

| Input | Source |
|---|---|
| Loan balances (`outstanding_balance`) | Observed — retrieved rows |
| Contractual rate on each loan (`int_rate`) | Observed — retrieved rows |
| Term and issue date (`term`, `issue_date`) | Observed — retrieved rows |
| Size of the deposit book | **Assumed** — 1.05× the loan book |
| Shape of the deposit book across buckets | **Assumed** — 55 / 25 / 15 / 5 percent |
| Rate paid to depositors | **Assumed** — 5% of the rate earned on the loans |

Every figure that depends on the deposit side inherits those three assumptions. The app and the PDF both restate them next to the numbers they produced, and the tool returns them as fields (`deposit_funding_ratio`, `deposit_rate_pass_through`) rather than burying them in code, so no reader has to guess which half is real.

The asset side is never assumed. If a query does not return `int_rate`, the earnings view is skipped entirely rather than substituted with a made-up yield — see `_earnings_view()`, which returns an empty dict instead of guessing.

---

## 3. Step one — when does each loan reprice?

A fixed-instalment loan does not really reprice at all. It pays the same rate until it is gone. So what gets bucketed is the point at which the money **rolls off** and could be lent again at whatever rate prevails then. For a fixed-rate term loan that is its remaining life:

```
months_elapsed   = (as_of_date - issue_date) / 30.44        # 30.44 = avg days/month
months_remaining = max(term_months - months_elapsed, 0)
```

Loans are then dropped into four buckets by `months_remaining`:

| Bucket | Months remaining |
|---|---|
| `0-3mo` | 0 ≤ m < 3 |
| `3-12mo` | 3 ≤ m < 12 |
| `1-3yr` | 12 ≤ m < 36 |
| `3yr+` | m ≥ 36 |

### The as-of date is the portfolio's date, not today's

This is a 2013–2018 book. Measured against the wall clock, every loan in it matured years ago, `months_remaining` collapses to 0 for all of them, and the gap table becomes a single bar — an artefact of *when the report was run*, not a property of the portfolio.

So the as-of date is **the latest `issue_date` in the retrieved rows** (`_resolve_as_of_date()`), which is when the snapshot was effectively taken. For the California example below that is `2018-12-01`. The app and PDF both print the date they used.

A related trap that this code has already been bitten by: `term` is stored as raw months (36/60) in the database but as a 0/1 code in the PD/LGD notebooks. Mapping unconditionally through the 0/1 lookup turned every real term into `NaN`, and unresolvable months silently fell through to the last bucket, so the entire book appeared to sit in `3yr+`. The mapping is now conditional, and an unresolvable term is treated as repricing **now** (the conservative direction) rather than never.

---

## 4. Step two — the deposit side

Three assumptions, each with a reason rather than a round number picked for looks.

**Size — 1.05× the loan book.** Deposits fund the loans *plus* the liquid assets a bank must carry alongside them, so a retail bank normally holds slightly more deposits than loans. 1.05 puts the loan-to-deposit ratio at 0.95, inside the internal 1.1 ceiling and in the range a real consumer bank runs.

```
total_deposits = total_loan_exposure × 1.05
```

**Shape — 55 / 25 / 15 / 5 across the four buckets.** Most retail deposits are demand or short-term accounts, so a deposit book is heavily weighted toward the near buckets. That weighting is what makes a normal bank liability-sensitive.

**Rate — 5% of what the loans earn.** At this portfolio's ~12.87% weighted-average contractual rate, a 5% pass-through implies a ~0.64% deposit rate, which is roughly what US retail savings accounts actually paid across the 2013–2018 window this data covers.

```
deposit_rate = portfolio_yield × 0.05
```

---

## 5. Step three — the gap

Two numbers per bucket, then the difference:

```
RSA(bucket)  = sum of exposure of loans repricing in that bucket
RSL(bucket)  = total_deposits × liability_weight(bucket)

periodic gap(bucket)   = RSA(bucket) - RSL(bucket)
cumulative gap(bucket) = sum of periodic gaps up to and including that bucket
net gap                = total RSA - total RSL
```

### Worked example — 113,008 California loans, as of 2018-12-01

| Bucket | RSA | RSL | Periodic gap | Cumulative gap |
|---|---|---|---|---|
| `0-3mo` | $217,281 | $697,094,005 | −$696,876,724 | −$696,876,724 |
| `3-12mo` | $19,496,005 | $316,860,911 | −$297,364,907 | −$994,241,630 |
| `1-3yr` | $666,342,781 | $190,116,547 | +$476,226,234 | −$518,015,396 |
| `3yr+` | $521,033,119 | $63,372,182 | +$457,660,937 | −$60,354,459 |
| **Total** | **$1,207,089,186** | **$1,267,443,645** | **−$60,354,459** | |

Check the first row against the formulas: `RSL = 1,267,443,645 × 0.55 = 697,094,005`, and the gap is `217,281 − 697,094,005 = −696,876,724`.

Why is `0-3mo` RSA only $217k out of $1.2bn? Because the as-of date is the *latest* issue date in the book. A loan issued in December 2018 has its full 36 or 60 months left. The only loans with under three months remaining are old 36-month loans issued around late 2015 that are nearly paid off. Near-term roll-off in a book of recently-issued fixed-term loans is genuinely tiny — and that is exactly why the near-bucket gap is so negative.

The net gap (−$60.4m) is just the 5% extra deposits: `1,207,089,186 − 1,207,089,186 × 1.05`. It is the least interesting number on the table. **The bucket pattern is the point**, not the total.

### Reading the "Repricing gap by bucket" chart

- **Bars** are the periodic gap — what reprices *inside* that one window. Green when positive (more assets than deposits reprice), coral when negative (more deposits than assets).
- **The teal line** is the cumulative gap — how far ahead or behind the book is by the *end* of that window. It is the line that explains the earnings numbers, because a bank's next twelve months depend on everything that has repriced by then, not on one window in isolation.
- **The zero line** is the reference: a bar or point below it means deposits reprice first in that window.

In the example the first two bars are deeply negative and the line falls to −$994m by the end of twelve months, then climbs back toward zero as the loan book finally rolls off. That shape — funding reprices now, lending reprices later — is the classic borrow-short-lend-long profile.

---

## 6. Step four — turning the gap into money

The gap says which side reprices first. It does not say what the book earns. That needs the earnings view:

```
interest_income  = sum over loans of (exposure × int_rate)      # int_rate/100, it is stored as a percent
portfolio_yield  = interest_income / total_RSA                  # = exposure-weighted average rate
deposit_rate     = portfolio_yield × 0.05
interest_expense = total_deposits × deposit_rate
NII              = interest_income - interest_expense           # net interest income
NIM              = NII / total_RSA                              # net interest margin
```

Note that `portfolio_yield` is not a simple average of the rate column — because income is summed exposure-by-exposure, it is automatically the **exposure-weighted** average rate. A $30,000 loan at 20% counts for six times as much as a $5,000 loan at 20%.

### Worked example, continued

```
interest_income  = $155,375,367
portfolio_yield  = 155,375,367 / 1,207,089,186   = 12.8719%
deposit_rate     = 12.8719% × 0.05               =  0.6436%
interest_expense = 1,267,443,645 × 0.006436      = $8,157,207
NII              = 155,375,367 - 8,157,207       = $147,218,160
NIM              = 147,218,160 / 1,207,089,186   = 12.1961%
```

A 12.2% net interest margin is very high for a bank — a real retail bank runs 2–4%. That is not an error in the arithmetic; it is what an unsecured consumer lending book at a ~12.9% average contractual rate, funded at 0.64%, actually looks like before credit losses. The losses are the other half of the story, and they are measured separately as expected loss.

---

## 7. Step five — earnings at risk (the bit with the actual maths)

This is the headline number of the whole section, so every step of it is spelled out below.

### The idea

If rates shift by some amount, each bucket's gap earns (or costs) that shift — but **only for the part of the next twelve months that comes after it reprices**. A balance that reprices in month 11 spends one month at the new rate, not twelve. So each bucket's contribution has to be weighted by time.

### The weights

The standard convention is to assume a bucket's balances reprice on average at the bucket's **midpoint**, and are then exposed for the rest of the year:

| Bucket | Midpoint | Months exposed in the next 12 | Weight |
|---|---|---|---|
| `0-3mo` | 1.5mo | 12 − 1.5 = 10.5 | 10.5 / 12 = **0.875** |
| `3-12mo` | 7.5mo | 12 − 7.5 = 4.5 | 4.5 / 12 = **0.375** |
| `1-3yr` | 24mo | 0 | **0.0** |
| `3yr+` | > 36mo | 0 | **0.0** |

The last two are zero because anything repricing beyond twelve months cannot affect a twelve-month earnings measure at all — by definition, not by assumption.

### The formula

```
ΔNII(shock) = Σ over buckets [ gap(bucket) × shock × weight(bucket) ]

where shock is the parallel shift as a fraction: +100 bps → +0.01
```

Four shocks are evaluated: **−200, −100, +100, +200 bps**. ±200bp is where supervisory rate-risk guidance conventionally starts.

**Earnings at risk (12m)** is then the single worst of those outcomes:

```
earnings_at_risk_12m = min(ΔNII over all four shocks, 0)
```

The `min(..., 0)` floor means it is never reported as a positive number: earnings at risk is a downside measure, and if no shock hurts, the answer is zero, not "we would make more".

### Worked example — the +100 bps column, all of it

Only the two near buckets contribute, since the other two carry weight 0.

```
0-3mo :  -696,876,724 × 0.01 × 0.875  =  -6,097,671
3-12mo:  -297,364,907 × 0.01 × 0.375  =  -1,115,118
1-3yr :  +476,226,234 × 0.01 × 0.0    =           0
3yr+  :  +457,660,937 × 0.01 × 0.0    =           0
                                         ----------
ΔNII(+100bp)                          =  -7,212,790
```

Then:

```
NII after   = 147,218,160 - 7,212,790  = $140,005,370
Change (%)  = -7,212,790 / 147,218,160 = -4.90%
```

### All four shocks

| Parallel shock | ΔNII (12m) | NII after | Change |
|---|---|---|---|
| −200 bps | +$14,425,579 | $161,643,739 | +9.80% |
| −100 bps | +$7,212,790 | $154,430,950 | +4.90% |
| +100 bps | −$7,212,790 | $140,005,370 | −4.90% |
| +200 bps | −$14,425,579 | $132,792,580 | −9.80% |

**Earnings at risk (12m) = −$14,425,579** — the +200bp case, the worst of the four. In plain terms: if rates rose two percentage points across the curve, this book would earn about $14.4m less over the following year, roughly a tenth of its net interest income.

### Reading the "Change in 12-month net interest income" chart

Four bars, one per shock, green above the zero line and coral below. For this book the bars are a clean staircase: negative on the right (rate rises hurt), positive on the left (rate cuts help), and exactly symmetric.

**The symmetry is a property of the method, not a finding.** ΔNII is linear in the shock — gap × shock × weight — so −200bp is always exactly the mirror image of +200bp. A real bank's rate exposure is not symmetric, because deposit rates do not follow cuts all the way down (deposit betas differ up and down) and borrowers prepay when rates fall. Neither behaviour is modelled here. Read the chart as "a first-order sensitivity", not as a forecast.

---

## 8. Liability-sensitive or asset-sensitive

```
gap_within_12m = gap(0-3mo) + gap(3-12mo)
is_liability_sensitive = gap_within_12m < 0
```

Judged on the **twelve-month window only**, deliberately. The total net gap is dominated by long-dated loan balances that cannot move the next year's earnings either way, so a book can have a positive total gap and still lose money when rates rise. In the example `gap_within_12m` is −$994m, so the book is liability-sensitive: *deposits reprice before the loans do, so a rate rise costs money before the book catches up.*

## 9. Loan-to-deposit ratio

```
loan_to_deposit = total_RSA / total_RSL = 1,207,089,186 / 1,267,443,645 = 0.9524
```

Which is just `1 / 1.05` — a direct consequence of the funding assumption, and the reason 1.05 was chosen rather than 1.0. It is checked against the internal `max_loan_to_deposit_ratio` limit of 1.1 in `Risk_Limits`, so it passes at 0.95. Above 1.0 the book would not be fully deposit-funded.

Basel does not impose a loan-to-deposit cap — it addresses funding stability through the Net Stable Funding Ratio instead — so this limit is labelled internal policy rather than given a manufactured citation.

---

## 10. What this deliberately does not do

| Not modelled | Why it matters |
|---|---|
| Economic value of equity (EVE) | Only earnings over 12 months are measured. A long-dated book can look fine on earnings and still lose a lot of economic value. |
| Prepayment behaviour | Borrowers repay early, especially when rates fall. Assumed away, so near-bucket roll-off is understated. |
| Non-parallel curve moves | Only parallel shifts. Real steepening/flattening is not tested. |
| Deposit betas | Deposits are assumed to pass through symmetrically and immediately. Real deposit rates lag rises and stick above zero on cuts. |
| Optionality, caps, floors | None present in this fixed-rate book, so nothing to model. |
| Credit losses | Separate measure — see expected loss and regulatory capital. NII here is pre-loss. |

## 11. Where it lives

| Item | Location |
|---|---|
| Buckets, weights, shocks, funding ratio, pass-through | Module constants, `tools/repricing_gap_tool.py` |
| As-of date resolution | `_resolve_as_of_date()` |
| Months remaining, all three accepted input shapes | `_resolve_months_remaining()` |
| Bucket assignment | `_assign_bucket()` |
| Yield, deposit rate, NII, NIM | `_earnings_view()` |
| Shocks and earnings at risk | `_rate_shocks()` |
| Result fields | `dmodels/repricing_gap_result.py` |
| Called by | `agents/interest_rate_concentration_agent.py`, in parallel with the credit-risk agent |
| Compliance check on LTD | `agents/compliance_agent.py`, `max_loan_to_deposit_ratio` |
| On-screen rendering | `render_interest_rate()`, `Frontend/views/risk_analysis.py` |
| PDF section 5 | `Frontend/memo.py` |

Related: [Concentration risk](README_concentration_risk.md), [PD model](README_behavioral_pd_model.md), [LGD model](README_lgd_model.md).
