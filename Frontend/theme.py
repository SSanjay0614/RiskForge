"""Shared theme, palette, glossary and formatting helpers for the RiskForge UI.

Imported by Frontend/app.py and by every module under Frontend/views/, so the
two surfaces cannot drift apart visually and the colour values live in exactly
one place -- the Plotly constants below mirror the CSS variables in STYLES.
"""

from pathlib import Path
import sys

import streamlit as st


def find_project_root() -> Path:
    """Walk up from this file until config.py is found (the project root)."""
    root = Path(__file__).resolve().parent
    while root != root.parent and not (root / "config.py").exists():
        root = root.parent
    return root


PROJECT_ROOT = find_project_root()

# Importing this module is enough to make the backend packages importable,
# so every page gets the same path bootstrap without repeating it.
if (PROJECT_ROOT / "config.py").exists() and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def _find_diagram() -> Path:
    """Git tracks the folder as 'Docs', but the README now points at 'docs'.
    Windows treats those as the same directory and Linux does not, so try both
    rather than betting on one and shipping a broken image to the deploy target.
    """
    for folder in ("Docs", "docs"):
        candidate = PROJECT_ROOT / folder / "RiskForge_Architecture.png"
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / "Docs" / "RiskForge_Architecture.png"


ARCHITECTURE_DIAGRAM = _find_diagram()


# --- Palette -------------------------------------------------------------
# Kept in sync with the CSS variables in STYLES below.

INK = "#f4f7f5"
MUTED = "#b8c7c1"
PAPER = "#111b1b"
GREEN = "#55d49b"
CORAL = "#ff7564"
TEAL = "#3fb8ad"
AMBER = "#e6b45c"
LIME = "#9fd67f"
GRID = "#2a4441"
RULE = "#8fada7"

# Ordered worst-to-best so an unrecognised tier label still gets a sane colour.
TIER_COLORS = {
    "low": GREEN,
    "medium": LIME,
    "high": AMBER,
    "very high": CORAL,
}

TIER_FALLBACK = [GREEN, LIME, AMBER, CORAL]


def tier_color(label: str, index: int = 0) -> str:
    return TIER_COLORS.get(str(label).strip().lower(), TIER_FALLBACK[index % len(TIER_FALLBACK)])


TERM_DEFINITIONS = {
    "PD": "Probability of Default - estimated likelihood that a loan defaults over a given period.",
    "LGD": "Loss Given Default - estimated fraction of exposure lost if a loan defaults.",
    "EAD": "Exposure at Default - outstanding balance at risk.",
    "EL": "Expected Loss - PD x LGD x EAD, the average loss expected on an exposure.",
    "HHI": "Herfindahl-Hirschman Index - higher values mean exposure is concentrated in fewer segments.",
    "RWA": "Risk Weighted Assets - The Basel III calculation output used to determine minimum required capital.",
    "Avg Risk Weight": "Avg Risk Weight - The Basel III risk multiplier.",
    "Capital reserve": "Capital Reserve - The minimum capital amount indicated by the Basel III 8% capital requirement calculation.",
    "Repricing gap": "Difference between rate-sensitive assets and liabilities within a time bucket.",
    "Risk tier": "Portfolio classification based on the model's estimated credit risk.",
    "Correlation (R)": "Asset correlation used by the Basel IRB risk-weight formula.",
    "Capital requirement (K)": "The Basel IRB capital requirement factor before applying EAD and the 12.5 multiplier.",
    "NII": "Net Interest Income - interest earned on the loan book less interest paid to depositors.",
    "NIM": "Net Interest Margin - net interest income as a percentage of rate-sensitive assets.",
    "Earnings at risk": "The largest fall in the next 12 months of net interest income across the modelled rate shocks.",
    "LTD": "Loan-to-Deposit ratio - loans divided by deposits. Above 1.0 the book is not fully deposit-funded.",
    "Deposit rate": "The interest rate paid to depositors, modelled here as a fixed share of the rate earned on the loans.",
}

# FeatureEngineeringTool reports its drops by internal key. Spell them out --
# "non_positive_dti" is not an explanation anybody outside the code can use.
DROP_REASON_LABELS = {
    "non_positive_dti": "Debt-to-income ratio missing or not positive",
    "missing_credit_report_cluster": "Incomplete credit-bureau history",
    "missing_inq_last_6mths": "Recent credit-inquiry count missing",
}


def drop_reason_label(reason: str) -> str:
    key = str(reason or "")
    return DROP_REASON_LABELS.get(key, key.replace("_", " ").capitalize())


PLOTLY_CONFIG = {
    "staticPlot": False,
    "displayModeBar": False,
    "scrollZoom": False,
    "doubleClick": False,
}


STYLES = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root { --ink: #f4f7f5; --muted: #b8c7c1; --paper: #111b1b; --mint: #193b35; --coral: #ff7564; --green: #55d49b; --teal: #3fb8ad; --amber: #e6b45c; --line: #31504a; }
.stApp { background: radial-gradient(circle at 10% 0%, #1b4038 0, transparent 34%), #111b1b; color: var(--ink); font-family: 'DM Sans', sans-serif; }
.main, [data-testid='stAppViewContainer'] { background-color: transparent; }
[data-testid='stHeader'] { background-color: rgba(17, 27, 27, .88); }
[data-testid='stSidebar'] { background-color: #0c1414; border-right: 1px solid var(--line); }
[data-testid='stMarkdownContainer'], [data-testid='stMetricLabel'], [data-testid='stMetricValue'], label, p, li { color: var(--ink); }
[data-testid='stCaptionContainer'], small { color: var(--muted) !important; }
.stChatMessage, [data-testid='stChatMessage'] { background: rgba(28, 45, 43, .82); border: 1px solid var(--line); box-sizing: border-box; margin-left: .5rem; margin-right: .5rem; width: calc(100% - 1rem); }
.stChatMessage > div:last-child, [data-testid='stChatMessage'] > div:last-child { padding-right: 4.5rem; box-sizing: border-box; }
.stChatInput { background: #182927; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif; letter-spacing: 0; }
.brand { padding: 1rem 0 0.6rem; border-bottom: 1px solid var(--line); margin-bottom: 1rem; }
.brand-kicker { color: var(--green); font-size: .74rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.brand h1 { margin: .15rem 0 0; font-size: 2.1rem; }
.brand p { color: var(--muted); margin: .25rem 0 0; }
.status { border-left: 4px solid var(--green); background: #193b35; padding: .65rem .8rem; border-radius: 4px; }
.breach { border-left: 4px solid var(--coral); background: #482824; padding: .7rem .8rem; border-radius: 4px; }
.compliant { border-left: 4px solid var(--green); background: #193b35; padding: .7rem .8rem; border-radius: 4px; }
[data-testid='stMetric'] { background: #1c2d2b; border: 1px solid var(--line); border-radius: 6px; padding: .8rem; min-height: 108px; }
[data-testid='stMetricValue'] { font-size: clamp(1.15rem, 2.2vw, 1.8rem); white-space: normal; overflow-wrap: anywhere; }

/* --- Pipeline trace strip (main page, under each risk report) --- */
.trace { display: flex; flex-wrap: wrap; gap: .35rem; align-items: stretch; margin: .2rem 0 .6rem; }
.trace-step { background: #1c2d2b; border: 1px solid var(--line); border-left: 3px solid var(--teal); border-radius: 4px; padding: .4rem .6rem; min-width: 96px; }
.trace-step.ok { border-left-color: var(--green); }
.trace-step.warn { border-left-color: var(--amber); }
.trace-step.skip { border-left-color: #4a625d; opacity: .55; }
.trace-name { display: block; font-size: .66rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--muted); }
.trace-val { display: block; font-size: .86rem; font-weight: 600; color: var(--ink); margin-top: .12rem; }
.trace-arrow { align-self: center; color: #4a625d; font-size: .95rem; }

/* --- Compliance summary banner --- */
.banner { border-radius: 5px; padding: .7rem .9rem; margin: .2rem 0 .5rem; border-left: 4px solid var(--green); background: #193b35; }
.banner.warn { border-left-color: var(--coral); background: #482824; }
.banner strong { font-size: 1rem; }
.banner span { color: var(--muted); font-size: .84rem; }

/* --- Small source/label pills --- */
.pill { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; padding: .14rem .45rem; border-radius: 3px; border: 1px solid var(--line); color: var(--muted); }
.pill.basel { border-color: #3f6f8a; color: #8fc7e6; }
.pill.internal { border-color: #6f5f3f; color: #e6cd8f; }
/* Observed vs assumed inputs -- used in the interest-rate view, where the asset
   side is read from the portfolio and the deposit side is a stated assumption. */
.pill.observed { border-color: #3f7a5c; color: #8fe6b8; }
.pill.assumed { border-color: #6f4a6a; color: #e0a8dc; }

/* --- Methodology page --- */
.method-lead { color: var(--muted); font-size: .95rem; line-height: 1.6; }
.method-note { border-left: 3px solid var(--teal); background: #162523; padding: .55rem .8rem; border-radius: 4px; color: var(--muted); font-size: .86rem; }
.stDataFrame, [data-testid='stTable'] { border: 1px solid var(--line); border-radius: 6px; }
@media (max-width: 640px) {
    .stChatMessage > div:last-child, [data-testid='stChatMessage'] > div:last-child { padding-right: .75rem; }
    .trace-step { min-width: 78px; }
}
</style>
"""


def inject_css() -> None:
    """Apply the shared stylesheet. Call once per page, after set_page_config."""
    st.markdown(STYLES, unsafe_allow_html=True)


# --- Value access + formatting -------------------------------------------
# State arrives either as Pydantic models or as plain dicts depending on
# whether it came straight out of the graph or through model_dump(), so every
# read goes through value() rather than assuming one shape.


def value(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def number(obj, key, default=0.0):
    raw = value(obj, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def money(amount):
    # Sign belongs outside the currency symbol -- "$-1,200" reads as a typo.
    return f"-${abs(amount):,.0f}" if amount < 0 else f"${amount:,.0f}"


def md_money(amount):
    """money() for a markdown context (st.markdown, st.caption, st.write).

    Streamlit reads '$' as the start of LaTeX math and the next '$' in the same
    block as its end, so two currency figures in one sentence silently swallow
    the words between them. st.metric values are not markdown-parsed and can
    use money() directly.
    """
    return money(amount).replace("$", "\\$")


def pct(amount):
    return f"{amount * 100:.2f}%"


def limit_value(amount) -> str:
    """Limits mix rates (0.05), ratios (1.1) and index scores (2500), so one
    fixed number of decimals is wrong for at least one of them. Four decimals
    below 1000 with trailing zeros trimmed, none above -- a flat ",.0f" printed
    the 1.1 loan-to-deposit ceiling as "1"."""
    if abs(amount) >= 1000:
        return f"{amount:,.0f}"
    return f"{amount:,.4f}".rstrip("0").rstrip(".")


# Risk_Limits metric_name values are snake_case identifiers; naive title-casing
# turns them into "Max Hhi 10000 Scale". Spell the display names out instead.
METRIC_LABELS = {
    "max_expected_loss_rate": "Expected loss rate",
    "max_hhi_10000_scale": "Concentration HHI (0-10000)",
    "max_loan_to_deposit_ratio": "Loan-to-deposit ratio",
    "pd_floor_retail_other": "PD floor - other retail",
    "lgd_floor_retail_unsecured_other": "LGD floor - unsecured other retail",
}


def metric_label(metric_name: str) -> str:
    key = str(metric_name or "")
    return METRIC_LABELS.get(key, key.replace("_", " ").capitalize())


def source_label(source: str) -> str:
    raw = str(source or "")
    return "Basel III" if raw.lower() == "basel_iii" else raw.replace("_", " ").title()


def configure_chart(figure):
    """Make a Plotly figure legible on the dark background."""
    # title_font below creates a layout.title object whether or not the caller ever
    # set title text, and a title carrying a font but no text reaches plotly.js as
    # title.text === undefined -- which it draws literally, as the word "undefined"
    # in the heading slot, in this very font. Every chart here titles itself except
    # the per-limit compliance bar, whose heading is already the markdown line above
    # it, so that was the only one showing it -- five times over, once per limit.
    # Defaulting the text to empty fixes it for any future title-less chart too,
    # and does not invent a heading for one that reads better without.
    if figure.layout.title.text is None:
        figure.update_layout(title_text="")
    figure.update_layout(
        dragmode=False,
        hovermode="closest",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=MUTED, family="DM Sans, sans-serif", size=12),
        title_font=dict(color=INK, family="Space Grotesk, sans-serif", size=15),
    )
    figure.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID)
    figure.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID)
    return figure
