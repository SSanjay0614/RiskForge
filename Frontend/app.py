from pathlib import Path
import sys

# Make the app runnable with `streamlit run Frontend/app.py` from any cwd.
project_root = Path(__file__).resolve().parent
while project_root != project_root.parent and not (project_root / "config.py").exists():
    project_root = project_root.parent
if (project_root / "config.py").exists() and str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from memory.state import RiskGraphState
from workflow.graph import graph
from workflow.nodes import credit_risk_agent, interest_rate_concentration_agent, compliance_agent


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
}

PLOTLY_CONFIG = {
    "staticPlot": False,
    "displayModeBar": False,
    "scrollZoom": False,
    "doubleClick": False,
}

st.set_page_config(page_title="RiskForge", page_icon="RF", layout="wide")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
    :root { --ink: #f4f7f5; --muted: #b8c7c1; --paper: #111b1b; --mint: #193b35; --coral: #ff7564; --green: #55d49b; }
    .stApp { background: radial-gradient(circle at 10% 0%, #1b4038 0, transparent 34%), #111b1b; color: var(--ink); font-family: 'DM Sans', sans-serif; }
    .main, [data-testid='stAppViewContainer'] { background-color: transparent; }
    [data-testid='stHeader'] { background-color: rgba(17, 27, 27, .88); }
    [data-testid='stSidebar'] { background-color: #0c1414; }
    [data-testid='stMarkdownContainer'], [data-testid='stMetricLabel'], [data-testid='stMetricValue'], label, p, li { color: var(--ink); }
    [data-testid='stCaptionContainer'], small { color: var(--muted) !important; }
    .stChatMessage, [data-testid='stChatMessage'] { background: rgba(28, 45, 43, .82); border: 1px solid #31504a; box-sizing: border-box; margin-left: .5rem; margin-right: .5rem; width: calc(100% - 1rem); }
    .stChatMessage > div:last-child, [data-testid='stChatMessage'] > div:last-child { padding-right: 4.5rem; box-sizing: border-box; }
    .stChatInput { background: #182927; }
    h1, h2, h3 { font-family: 'Space Grotesk', sans-serif; letter-spacing: 0; }
    .brand { padding: 1rem 0 0.6rem; border-bottom: 1px solid #31504a; margin-bottom: 1rem; }
    .brand-kicker { color: var(--green); font-size: .74rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
    .brand h1 { margin: .15rem 0 0; font-size: 2.1rem; }
    .brand p { color: var(--muted); margin: .25rem 0 0; }
    .status { border-left: 4px solid var(--green); background: #193b35; padding: .65rem .8rem; border-radius: 4px; }
    .breach { border-left: 4px solid var(--coral); background: #482824; padding: .7rem .8rem; border-radius: 4px; }
    .compliant { border-left: 4px solid var(--green); background: #193b35; padding: .7rem .8rem; border-radius: 4px; }
    [data-testid='stMetric'] { background: #1c2d2b; border: 1px solid #31504a; border-radius: 6px; padding: .8rem; min-height: 108px; }
    [data-testid='stMetricValue'] { font-size: clamp(1.15rem, 2.2vw, 1.8rem); white-space: normal; overflow-wrap: anywhere; }
    @media (max-width: 640px) {
        .stChatMessage > div:last-child, [data-testid='stChatMessage'] > div:last-child { padding-right: .75rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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
    return f"${amount:,.0f}"


def pct(amount):
    return f"{amount * 100:.2f}%"


def metric(label, amount, help_key, delta=None):
    st.metric(label, amount, delta=delta, help=TERM_DEFINITIONS[help_key])


def empty_chart(message):
    figure = go.Figure()
    figure.add_annotation(text=message, x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    figure.update_layout(height=260, margin=dict(l=10, r=10, t=20, b=10))
    return figure


def configure_chart(figure):
    figure.update_layout(dragmode=False, hovermode="closest")
    return figure


def render_distribution(distribution):
    if not distribution:
        st.info("Risk tier distribution is unavailable for these rows.")
        return
    labels = list(distribution)
    values = [number(distribution, label) * 100 for label in labels]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=["#237a57", "#80a96e", "#e0a04f", "#d95d4d"]))
    fig.update_layout(title="Risk tier distribution", xaxis_title="Portfolio share (%)", yaxis_title=None, height=300, margin=dict(l=10, r=10, t=48, b=10))
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG)


def render_concentration(title, result):
    if not result:
        st.info(f"{title} is unavailable for these rows.")
        return
    shares = value(result, "segment_shares", {}) or {}
    if not shares:
        st.info(f"{title} has no segment values to display.")
        return
    labels = list(shares)
    values = [number(shares, label) * 100 for label in labels]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color="#338a82"))
    fig.update_layout(
        title=f"{title} | HHI {number(result, 'hhi_score_10000_scale'):,.0f} | {value(result, 'diversification_level', 'Unknown')}",
        xaxis_title="Exposure share (%)", yaxis_title=None, height=max(280, 30 * len(labels) + 90), margin=dict(l=10, r=10, t=65, b=10),
    )
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG)


def render_repricing(result):
    if not result:
        st.info("Repricing gap is unavailable for these rows.")
        return
    buckets = value(result, "buckets", []) or []
    labels = [value(bucket, "bucket_label", "Unknown") for bucket in buckets]
    gaps = [number(bucket, "gap") for bucket in buckets]
    if not labels:
        st.info("No repricing buckets were returned.")
        return
    net_gap = number(result, "net_gap")
    assets = number(result, "total_rate_sensitive_assets")
    liabilities = number(result, "total_rate_sensitive_liabilities")
    gap_columns = st.columns(3)
    with gap_columns[0]:
        st.metric("Net Gap", f"{net_gap:,.2f}", help=TERM_DEFINITIONS["Repricing gap"])
    with gap_columns[1]:
        st.metric("Rate-Sensitive Assets", f"{assets:,.2f}", help="Assets whose value or income changes as interest rates change.")
    with gap_columns[2]:
        st.metric("Rate-Sensitive Liabilities", f"{liabilities:,.2f}", help="Liabilities whose value or cost changes as interest rates change.")
    st.caption(f"Net gap = rate-sensitive assets - rate-sensitive liabilities = {assets:,.2f} - {liabilities:,.2f} = {net_gap:,.2f}")
    fig = go.Figure(go.Bar(x=labels, y=gaps, marker_color=["#237a57" if gap >= 0 else "#d95d4d" for gap in gaps]))
    fig.add_hline(y=0, line_color="#17232b", line_width=1)
    fig.update_layout(title="Repricing gap", yaxis_title="Gap", height=320, margin=dict(l=10, r=10, t=48, b=10))
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG)
    if value(result, "liabilities_are_synthetic", False):
        st.caption("Liability figures are synthetic assumptions documented by the backend.")


def render_flag(flag):
    breached = bool(value(flag, "breached", False))
    css_class = "breach" if breached else "compliant"
    indicator = "⚠ Breached" if breached else "✓ Within limit"
    source_value = value(flag, "source", "")
    source = "Basel III" if source_value.lower() == "basel_iii" else source_value.replace("_", " ").title()
    st.markdown(f"<div class='{css_class}'><strong>{indicator}</strong> &nbsp; {value(flag, 'metric_name', 'Metric').replace('_', ' ').title()} &nbsp; <small>{source}</small></div>", unsafe_allow_html=True)
    left, right = st.columns(2)
    left.write(f"Actual: **{number(flag, 'value'):,.4f}**")
    right.write(f"Threshold: **{number(flag, 'threshold'):,.4f}**")
    actual = number(flag, "value")
    threshold = number(flag, "threshold")
    maximum = max(abs(actual), abs(threshold), 1e-9) * 1.2
    fig = go.Figure(go.Bar(x=[actual], y=["Actual"], orientation="h", marker_color="#d95d4d" if breached else "#237a57"))
    fig.add_vline(x=threshold, line_color="#17232b", line_width=2, annotation_text="threshold", annotation_position="top")
    fig.update_xaxes(range=[0, maximum])
    fig.update_layout(height=130, margin=dict(l=10, r=10, t=25, b=15), showlegend=False)
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG)
    citation = value(flag, "citation")
    if citation:
        st.caption(f"Citation: {citation}")


def run_risk_analysis(result_state):
    state = RiskGraphState.model_validate(result_state)
    state = state.model_copy(update={"requires_risk_analysis": True}, deep=False)
    state = state.model_copy(update=credit_risk_agent.run(state), deep=False)
    state = state.model_copy(update=interest_rate_concentration_agent.run(state), deep=False)
    state = state.model_copy(update=compliance_agent.run(state), deep=False)
    return state


def render_simple(result_state, data_result, button_key):
    row_count = int(value(result_state, "row_count", 0) or 0)
    if row_count == 0:
        st.info("No data was found for that question.")
    else:
        noun = "row" if row_count == 1 else "rows"
        st.markdown(f"<div class='status'><strong>{row_count:,} {noun} pulled</strong><br><span>Data was retrieved from the loan portfolio.</span></div>", unsafe_allow_html=True)
        st.write("Would you like to perform a risk analysis on these rows?")
        if st.button("Run risk analysis", key=button_key, type="primary"):
            with st.spinner("Running credit, interest-rate, concentration, and compliance analysis..."):
                try:
                    result_state = run_risk_analysis(result_state)
                except Exception as error:
                    st.error(
                        "Risk analysis needs detailed loan-level columns, but this query returned "
                        f"a summary result. {error}"
                    )
                    return result_state, False
            return result_state, True
    if value(data_result, "sql_query"):
        with st.expander("Query details"):
            st.code(value(data_result, "sql_query"), language="sql")
    return result_state, False


def render_risk_report(result_state):
    credit = value(result_state, "credit_metrics", {}) or {}
    rate = value(result_state, "rate_metrics", {}) or {}
    compliance = value(result_state, "compliance_result", {}) or {}
    capital = value(compliance, "regulatory_capital", {}) or {}
    data_result = value(result_state, "data_agent_result")
    sql_query = value(data_result, "sql_query") or value(result_state, "sql_query", "")
    loan_count = int(number(credit, "loan_count", value(result_state, "row_count", 0)) or 0)

    st.markdown(f"<div class='status'><strong>Risk report generated from {loan_count:,} loans</strong></div>", unsafe_allow_html=True)

    card_data = [
        ("Total Exposure", money(number(credit, "total_exposure")), "EAD"),
        ("Avg PD", pct(number(credit, "exposure_weighted_avg_pd")), "PD"),
        ("Avg LGD", pct(number(credit, "exposure_weighted_avg_lgd")), "LGD"),
        ("Expected Loss Rate", pct(number(credit, "expected_loss_rate")), "EL"),
        ("Expected Loss", money(number(credit, "total_expected_loss")), "EL"),
        ("Avg Risk Weight", f"{number(capital, 'avg_risk_weight_pct'):.2f}%", "Avg Risk Weight"),
        ("Total RWA", money(number(capital, "total_rwa")), "RWA"),
        ("Capital Reserve", money(number(capital, "total_capital_requirement_8pct")), "Capital reserve"),
    ]
    for row_start, row_end in ((0, 4), (4, 8)):
        cards = st.columns(row_end - row_start)
        for column, (label, display, term) in zip(cards, card_data[row_start:row_end]):
            with column:
                metric(label, display, term)

    st.subheader("Credit profile")
    render_distribution(value(credit, "risk_tier_distribution", {}))

    st.subheader("Portfolio concentration")
    purpose, region = st.columns(2)
    with purpose:
        render_concentration("By purpose", value(rate, "concentration_by_purpose"))
    with region:
        render_concentration("By region", value(rate, "concentration_by_region"))

    st.subheader("Interest-rate sensitivity")
    render_repricing(value(rate, "repricing_gap"))

    flags = value(compliance, "flags", []) or []
    with st.expander(f"Compliance checks ({len(flags)})", expanded=bool(value(compliance, "any_breach", False))):
        if not flags:
            st.info("No compliance checks were returned.")
        for flag in flags:
            render_flag(flag)
            st.divider()
    citation = value(compliance, "regulatory_capital_citation")
    if citation:
        with st.expander("Regulatory Capital Methodology"):
            st.info(citation)
    if sql_query:
        with st.expander("SQL Query Used"):
            st.code(sql_query, language="sql")


def render_response(result_state, turn_key=None):
    data_result = value(result_state, "data_agent_result")
    data_available = bool(value(result_state, "data_available", False))
    retries = int(value(data_result, "retries_used", 0) or 0)
    success = bool(value(data_result, "success", False))
    if not data_available and not success and retries == 0:
        reason = value(result_state, "guard_reason") or value(data_result, "message", "No matching portfolio data was available.")
        st.warning(f"I couldn't answer that from the loan portfolio data: {reason}")
    elif not data_available and retries > 0:
        st.error(f"I tried a few times but couldn't retrieve valid data for this question. {value(data_result, 'message', '')}")
    elif not bool(value(result_state, "requires_risk_analysis", True)):
        return render_simple(result_state, data_result, f"risk-analysis-{turn_key}")
    else:
        render_risk_report(result_state)
    return result_state, False


def scroll_to_latest_message():
    components.html(
        """
        <script>
        const scrollLatestMessage = () => {
            const messages = window.parent.document.querySelectorAll('[data-testid="stChatMessage"]');
            const latest = messages[messages.length - 1];
            if (latest) {
                latest.scrollIntoView({ behavior: "smooth", block: "end" });
            }
        };
        setTimeout(scrollLatestMessage, 150);
        setTimeout(scrollLatestMessage, 500);
        </script>
        """,
        height=0,
    )


st.markdown("<div class='brand'><div class='brand-kicker'>Portfolio intelligence</div><h1>RiskForge</h1><p>Ask a question about the loan book and receive an auditable risk view.</p></div>", unsafe_allow_html=True)

if "conversation" not in st.session_state:
    st.session_state.conversation = []

for turn_index, turn in enumerate(st.session_state.conversation):
    with st.chat_message("user"):
        st.write(turn["query"])
    with st.chat_message("assistant"):
        updated_result, changed = render_response(turn["result"], turn_index)
        if changed:
            turn["result"] = updated_result
            turn["risk_analysis_done"] = True
            render_response(updated_result, turn_index)

query = st.chat_input("Ask about exposure, expected loss, concentration, or compliance...")
if query:
    with st.chat_message("user"):
        st.write(query)
    with st.chat_message("assistant"):
        with st.spinner("Running the RiskForge analysis..."):
            result_state = graph.invoke(RiskGraphState(query=query, max_retries=3))
        render_response(result_state, len(st.session_state.conversation))
    st.session_state.conversation.append({"query": query, "result": result_state})
    scroll_to_latest_message()