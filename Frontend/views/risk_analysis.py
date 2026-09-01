"""RiskForge risk analysis -- the chat interface.

Ask a question about the loan book, get an auditable risk view. Shared palette,
CSS and formatting helpers live in theme.py so this page and the Methodology
page cannot drift; the downloadable PDF memo is built in memo.py. Declared as a
page by Frontend/app.py, which owns set_page_config and the stylesheet.

Nothing here computes a risk figure -- every number is read out of the state the
LangGraph workflow returns, and only aggregated metrics are ever rendered.
"""

from pathlib import Path
import sys
import time

# Streamlit puts the entrypoint's folder on sys.path; this keeps the module
# importable when it is run or imported directly too.
FRONTEND_DIR = Path(__file__).resolve().parent.parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# theme also bootstraps sys.path, so it is imported before the backend packages.
from theme import (
    AMBER,
    CORAL,
    GREEN,
    PLOTLY_CONFIG,
    RULE,
    TEAL,
    TERM_DEFINITIONS,
    configure_chart,
    drop_reason_label,
    limit_value,
    md_money,
    metric_label,
    money,
    number,
    pct,
    source_label,
    tier_color,
    value,
)

import backend  # noqa: E402
from memo import build_risk_memo, memo_filename  # noqa: E402

SAMPLE_QUESTIONS = [
    "What is our expected loss and concentration risk for California loans?",
    "Show the risk profile of grade D and E loans issued in 2017.",
    "How many loans have sub_grade B3?",
    "What is the regulatory capital requirement for loans under $10,000?",
]


def metric(label, amount, help_key, delta=None):
    st.metric(label, amount, delta=delta, help=TERM_DEFINITIONS[help_key])


# --- Pipeline trace -------------------------------------------------------
# Reconstructed from the returned state rather than instrumented inside the
# graph, so the interface stays read-only. It answers the question a reviewer
# actually asks: which stages ran, how many retries it took, and what got
# skipped -- the traceability half of "auditable, source-attributed".


def trace_steps(result_state, elapsed=None):
    data_result = value(result_state, "data_agent_result")
    retries = int(value(data_result, "retries_used", 0) or 0)
    row_count = int(value(result_state, "row_count", 0) or 0)
    data_available = bool(value(result_state, "data_available", False))
    sql_query = value(data_result, "sql_query") or value(result_state, "sql_query", "") or ""
    credit = value(result_state, "credit_metrics") or {}
    rate = value(result_state, "rate_metrics") or {}
    compliance = value(result_state, "compliance_result") or {}

    # The guard writes its reason on BOTH paths -- "answerable because..." as well
    # as "not answerable because..." -- so a non-empty guard_reason means the
    # guard ran, not that it blocked. What distinguishes a block is that no SQL
    # was ever generated: data_available also goes false when the retry loop
    # gives up, but by then sql_query is populated.
    guard_blocked = bool(value(result_state, "guard_blocked",
                                not data_available and not sql_query))

    steps = [("Schema guard", "Blocked", "warn") if guard_blocked else ("Schema guard", "Passed", "ok")]

    if guard_blocked:
        steps.append(("SQL", "Not attempted", "skip"))
    else:
        attempts = retries + 1
        label = "1 attempt" if attempts == 1 else f"{attempts} attempts"
        if value(result_state, "pipeline_error"):
            # describe_execution returns an error and a cause, never a counter,
            # and the history that holds one also holds an inline row.
            label = "no valid result"
        elif not data_available:
            label = f"{label}, no valid result"
        steps.append(("SQL", label, "ok" if retries == 0 and data_available else "warn"))

    if not data_available:
        steps.append(("Rows", "None", "skip"))
    else:
        suffix = " (capped)" if value(result_state, "truncated", False) else ""
        steps.append(("Rows", f"{row_count:,}{suffix}", "warn" if suffix else "ok"))

    if not credit:
        steps.append(("Credit risk", "Skipped", "skip"))
    else:
        scored = int(number(credit, "loan_count", 0) or 0)
        retrieved = int(number(credit, "rows_retrieved", scored) or scored)
        # Say "N of M" only when they differ, so the common case stays terse and
        # the uncommon case explains itself instead of looking like a mismatch.
        detail = f"{scored:,} scored" if scored == retrieved else f"{scored:,} of {retrieved:,} scored"
        steps.append(("Credit risk", detail, "ok" if scored == retrieved else "warn"))

    steps.append(("Rate & conc.", "Computed", "ok") if rate else ("Rate & conc.", "Skipped", "skip"))

    flags = value(compliance, "flags") or []
    checks = "1 check" if len(flags) == 1 else f"{len(flags)} checks"
    if not flags:
        steps.append(("Compliance", "Skipped", "skip"))
    elif value(compliance, "any_breach", False):
        steps.append(("Compliance", f"{checks}, breach", "warn"))
    else:
        steps.append(("Compliance", f"{checks}, clear", "ok"))

    if elapsed is not None:
        steps.append(("Elapsed", f"{elapsed:.1f}s", "ok"))
    return steps



def render_trace(result_state, elapsed=None):
    cells = []
    for index, (name, detail, state) in enumerate(trace_steps(result_state, elapsed)):
        if index:
            cells.append("<span class='trace-arrow'>&rsaquo;</span>")
        cells.append(
            f"<div class='trace-step {state}'><span class='trace-name'>{name}</span>"
            f"<span class='trace-val'>{detail}</span></div>"
        )
    st.markdown(f"<div class='trace'>{''.join(cells)}</div>", unsafe_allow_html=True)


# --- Charts ---------------------------------------------------------------
#
# Every chart takes a `key`, and it is not optional in practice. Streamlit
# derives an element's identity from its type plus its parameters when no key is
# given, so two charts built from the same numbers hash to the same id -- and the
# second one raises StreamlitDuplicateElementId rather than rendering. That is
# not a hypothetical: the conversation re-renders every past turn on each run, so
# asking the same question twice puts two identical risk-tier distributions on
# the page and the app dies on the second. The keys below are all derived from
# the turn index, which makes them unique across turns and stable across reruns
# -- stable matters, because a key that changed between runs would reset the
# chart's own client-side state (zoom, hidden traces) on every interaction.


def render_distribution(distribution, key=None):
    if not distribution:
        st.info("Risk tier distribution is unavailable for these rows.")
        return
    labels = list(distribution)
    values = [number(distribution, label) * 100 for label in labels]
    # Colour by tier name, not by position, so "High" is amber even when a tier
    # is missing from the returned distribution.
    colors = [tier_color(label, index) for index, label in enumerate(labels)]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=colors))
    fig.update_layout(
        title="Risk tier distribution", xaxis_title="Portfolio share (%)", yaxis_title=None,
        height=300, margin=dict(l=10, r=10, t=48, b=10),
    )
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG, key=key)


def render_concentration(title, result, key=None):
    if not result:
        st.info(f"{title} is unavailable for these rows.")
        return
    shares = value(result, "segment_shares", {}) or {}
    if not shares:
        st.info(f"{title} has no segment values to display.")
        return
    labels = list(shares)
    values = [number(shares, label) * 100 for label in labels]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=TEAL))
    fig.update_layout(
        title=(
            f"{title} | HHI {number(result, 'hhi_score_10000_scale'):,.0f} | "
            f"{value(result, 'diversification_level', 'Unknown')}"
        ),
        xaxis_title="Exposure share (%)", yaxis_title=None,
        height=max(280, 30 * len(labels) + 90), margin=dict(l=10, r=10, t=65, b=10),
    )
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG, key=key)


def render_rate_shocks(shocks, key=None):
    """Earnings at risk: what a parallel move in the curve does to the next
    twelve months of net interest income."""
    labels = [f"{'+' if number(s, 'shock_bps') > 0 else ''}{number(s, 'shock_bps'):.0f}bp" for s in shocks]
    changes = [number(s, "net_interest_income_change") for s in shocks]
    fig = go.Figure(go.Bar(
        x=labels, y=changes,
        marker_color=[GREEN if change >= 0 else CORAL for change in changes],
        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_color=RULE, line_width=1)
    fig.update_layout(
        title="Change in 12-month net interest income",
        xaxis_title="Parallel rate shock", yaxis_title="Change in NII",
        height=300, margin=dict(l=10, r=10, t=48, b=10),
    )
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG, key=key)


def render_gap_chart(labels, gaps, cumulative, key=None):
    """Periodic gap as bars, cumulative gap as a line.

    The periodic gap says what reprices inside each window; the cumulative line
    says how much of the book has repriced by the end of it, which is the shape
    that actually explains the earnings numbers above.
    """
    fig = go.Figure(go.Bar(
        x=labels, y=gaps, name="Periodic gap",
        marker_color=[GREEN if gap >= 0 else CORAL for gap in gaps],
        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=cumulative, name="Cumulative gap", mode="lines+markers",
        line=dict(color=TEAL, width=2), marker=dict(size=7),
        hovertemplate="%{x} cumulative: %{y:$,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_color=RULE, line_width=1)
    fig.update_layout(
        title="Repricing gap by bucket", xaxis_title="Time to repricing", yaxis_title="Gap",
        height=300, margin=dict(l=10, r=10, t=48, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
    )
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG, key=key)


def render_interest_rate(result, key=None):
    """Repricing gap plus the earnings view built on top of it.

    A gap table on its own is close to unreadable: it reports two large numbers
    that nearly cancel and leaves the reader to guess what the difference costs.
    The figures below turn it into money -- what the book earns, what the
    depositors funding it are paid, and how much of the difference a rate move
    would take away.
    """
    if not result:
        st.info("Interest-rate sensitivity is unavailable for these rows.")
        return

    buckets = value(result, "buckets", []) or []
    if not buckets:
        st.info("No repricing buckets were returned.")
        return

    labels = [value(bucket, "bucket_label", "Unknown") for bucket in buckets]
    gaps = [number(bucket, "gap") for bucket in buckets]
    cumulative = [number(bucket, "cumulative_gap") for bucket in buckets]

    assets = number(result, "total_rate_sensitive_assets")
    liabilities = number(result, "total_rate_sensitive_liabilities")
    net_gap = number(result, "net_gap")
    earnings_available = bool(value(result, "earnings_view_available", False))
    shocks = value(result, "rate_shocks", []) or []

    # The deposit-side fields default to 0.0 on the result model, so a zero here
    # means the result did not report them -- not that the bank funds itself with
    # nothing at a zero rate. Printing "0.00x" and "0.00%" as stated assumptions
    # would be worse than saying nothing, so gate every sentence that reads them.
    funding_ratio = number(result, "deposit_funding_ratio")
    pass_through = number(result, "deposit_rate_pass_through")
    assumptions_reported = funding_ratio > 0.0 and pass_through > 0.0

    if earnings_available:
        income = number(result, "interest_income_annual")
        expense = number(result, "interest_expense_annual")
        net_income = number(result, "net_interest_income_annual")
        worst = number(result, "earnings_at_risk_12m")
        cards = st.columns(4)
        with cards[0]:
            st.metric("Net interest income", money(net_income), help=TERM_DEFINITIONS["NII"])
        with cards[1]:
            st.metric("Net interest margin", pct(number(result, "net_interest_margin")),
                      help=TERM_DEFINITIONS["NIM"])
        with cards[2]:
            st.metric("Earnings at risk (12m)", money(worst),
                      help=TERM_DEFINITIONS["Earnings at risk"])
        with cards[3]:
            st.metric("Loan-to-deposit", f"{number(result, 'loan_to_deposit_ratio'):.2f}x",
                      help=TERM_DEFINITIONS["LTD"])
        pass_through_clause = (
            f"{pct(pass_through)} of that, a {pct(number(result, 'deposit_rate'))} deposit rate, "
            if assumptions_reported
            else f"a {pct(number(result, 'deposit_rate'))} deposit rate, "
        )
        st.caption(
            f"The book earns {pct(number(result, 'portfolio_yield'))} on average, from each loan's "
            f"own contractual rate. Depositors funding it receive "
            f"{pass_through_clause}so on a "
            f"{md_money(liabilities)} deposit book the interest paid out is "
            f"{md_money(expense)} a year against {md_money(income)} earned -- leaving "
            f"{md_money(net_income)} of net interest income."
        )
        sensitivity = (
            "Liabilities reprice before the loans do, so a rate rise costs money before the "
            "book catches up."
            if value(result, "is_liability_sensitive", False)
            else "Assets reprice before the deposits do, so a rate rise adds income in the near term."
        )
        st.caption(sensitivity)
    else:
        st.caption(
            "The earnings view needs each loan's contractual interest rate, which this query "
            "did not return, so only the gap table below is available."
        )

    left, right = st.columns(2)
    with left:
        if shocks:
            render_rate_shocks(shocks, key="%s-shocks" % key)
        else:
            render_gap_chart(labels, gaps, cumulative, key="%s-gap" % key)
    with right:
        if shocks:
            render_gap_chart(labels, gaps, cumulative, key="%s-gap" % key)
        else:
            st.metric("Net gap", money(net_gap), help=TERM_DEFINITIONS["Repricing gap"])

    with st.expander("Interest-rate inputs -- what is observed and what is assumed"):
        st.markdown(
            "<span class='pill observed'>Observed</span> &nbsp; Loan balances, contractual "
            "interest rates, terms and issue dates, all read from the retrieved rows.<br>"
            "<span class='pill assumed'>Assumed</span> &nbsp; The entire deposit side. This "
            "portfolio holds loans, not deposits, so there is no liability data to read.",
            unsafe_allow_html=True,
        )
        as_of = value(result, "as_of_date", "")
        if assumptions_reported:
            deposit_bullets = (
                f"- Deposit book sized at **{funding_ratio:.2f}x** the loan "
                f"book ({md_money(assets)} of loans, {md_money(liabilities)} of deposits), spread "
                "across the four buckets at **55 / 25 / 15 / 5 percent** -- the short-weighted "
                "shape of a retail deposit book.\n"
                f"- Depositors receive **{pct(pass_through)}** of the "
                "rate earned on the loans. Nothing in the data sets this; it is a stated "
                "assumption.\n"
            )
        else:
            deposit_bullets = (
                f"- The deposit book is modelled at {md_money(liabilities)} against "
                f"{md_money(assets)} of loans, but this result did not report the funding ratio "
                "or the deposit rate pass-through it used, so they are not restated here. If the "
                "net gap reads exactly zero, the analysis was produced by an earlier version of "
                "the repricing tool -- restart the app to pick up the current one.\n"
            )
        st.markdown(
            deposit_bullets +
            "- Asset repricing is approximated as the months remaining on each fixed-rate term. "
            "A fixed instalment loan does not truly reprice, so the bucket is really when the "
            "cash flow rolls off.\n"
            f"- Measured as of **{as_of or 'the portfolio snapshot date'}**, the latest issue date "
            "in the retrieved rows, rather than today -- against today's date this 2013-2018 book "
            "has entirely matured and every bucket but the first would be empty.\n"
            "- Shocks are parallel shifts applied to the gap in each bucket, weighted by how much "
            "of the next twelve months the repriced balance is exposed for. No prepayment "
            "behaviour, no non-parallel curve moves, and no economic-value-of-equity measure."
        )
        st.caption(
            "Every figure above that depends on the deposit side inherits these assumptions, "
            "including the loan-to-deposit compliance check. See Methodology & Transparency, "
            "section 7."
        )



def render_flag(flag, key=None):
    breached = bool(value(flag, "breached", False))
    css_class = "breach" if breached else "compliant"
    indicator = "⚠ Breached" if breached else "✓ Within limit"
    source = value(flag, "source", "")
    # A ceiling and a floor are read in opposite directions; saying which one
    # this is stops "actual below threshold" from looking like a pass by default.
    test = "Ceiling" if value(flag, "direction", "") == "max" else "Floor"
    pill = "basel" if str(source).lower() == "basel_iii" else "internal"
    st.markdown(
        f"<div class='{css_class}'><strong>{indicator}</strong> &nbsp; "
        f"{metric_label(value(flag, 'metric_name', 'Metric'))} &nbsp; "
        f"<span class='pill {pill}'>{source_label(source)}</span> &nbsp; <small>{test}</small></div>",
        unsafe_allow_html=True,
    )
    actual = number(flag, "value")
    threshold = number(flag, "threshold")
    left, right = st.columns(2)
    left.write(f"Actual: **{limit_value(actual)}**")
    right.write(f"Threshold: **{limit_value(threshold)}**")
    maximum = max(abs(actual), abs(threshold), 1e-9) * 1.2
    fig = go.Figure(go.Bar(x=[actual], y=["Actual"], orientation="h", marker_color=CORAL if breached else GREEN))
    fig.add_vline(x=threshold, line_color=AMBER, line_width=2, annotation_text="threshold", annotation_position="top")
    fig.update_xaxes(range=[0, maximum])
    fig.update_layout(height=130, margin=dict(l=10, r=10, t=25, b=15), showlegend=False)
    st.plotly_chart(configure_chart(fig), use_container_width=True, config=PLOTLY_CONFIG, key=key)
    citation = value(flag, "citation")
    if citation:
        st.caption(f"Citation: {citation}")


def render_scoring_coverage(credit, row_count):
    """Explain a scored count that is lower than the retrieved row count.

    Two numbers that disagree and say nothing about why is the fastest way to
    lose a reader's trust in the rest of the report, so the gap is named and
    attributed rather than left to be noticed.
    """
    scored = int(number(credit, "loan_count", 0) or 0)
    retrieved = int(number(credit, "rows_retrieved", row_count) or row_count)
    dropped = int(number(credit, "rows_dropped", 0) or 0)
    if dropped <= 0 or retrieved <= 0:
        return
    reasons = value(credit, "dropped_reason_counts") or {}
    named = [
        f"{drop_reason_label(reason)} ({int(count):,})"
        for reason, count in reasons.items()
        if int(count or 0) > 0
    ]
    share = dropped / retrieved
    st.markdown(
        f"<div class='method-note'><strong>{scored:,} of {retrieved:,} retrieved loans were "
        f"scored</strong> ({pct(share)} excluded). The PD and LGD models require a complete "
        "feature row; a loan missing one of the inputs is excluded rather than scored on an "
        f"imputed guess.{'<br>Excluded for: ' + ', '.join(named) if named else ''}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Exposure, expected loss and regulatory capital are totals over the scored loans only, "
        "so they are consistent with each other. Concentration and the interest-rate view are "
        "computed on all retrieved rows, since neither needs a model input."
    )


def render_compliance_banner(compliance):
    """One-line verdict above the expander -- the report should not require
    opening an accordion to learn whether a limit was breached."""
    flags = value(compliance, "flags", []) or []
    if not flags:
        return
    breached = [flag for flag in flags if value(flag, "breached", False)]
    total = "1 limit" if len(flags) == 1 else f"{len(flags)} limits"
    if breached:
        names = ", ".join(metric_label(value(flag, "metric_name", "")) for flag in breached)
        st.markdown(
            f"<div class='banner warn'><strong>{len(breached)} of {total} breached</strong>"
            f"<br><span>{names}</span></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div class='banner'><strong>All {total} within tolerance</strong>"
            "<br><span>Internal risk-appetite ceilings and Basel III input floors both satisfied.</span></div>",
            unsafe_allow_html=True,
        )


# --- PDF memo -------------------------------------------------------------
# Keyed on the figures rather than on the state object: identical metrics mean an
# identical memo, and rebuilding one costs a few hundred milliseconds of chart
# rendering that would otherwise repeat on every Streamlit rerun.


@st.cache_data(show_spinner=False)
def cached_memo(cache_key: str, _result_state) -> bytes:
    return build_risk_memo(_result_state)


def memo_cache_key(result_state) -> str:
    credit = value(result_state, "credit_metrics") or {}
    capital = value(value(result_state, "compliance_result") or {}, "regulatory_capital") or {}
    return "|".join(str(part) for part in (
        value(result_state, "query", ""),
        value(result_state, "sql_query", ""),
        number(credit, "loan_count"),
        number(credit, "total_exposure"),
        number(credit, "total_expected_loss"),
        number(capital, "total_rwa"),
    ))


def render_memo_download(result_state, button_key):
    try:
        payload = cached_memo(memo_cache_key(result_state), result_state)
    except Exception as error:
        st.caption(f"The PDF memo could not be generated for this report: {error}")
        return
    st.download_button(
        "Download risk memo (PDF)",
        data=payload,
        file_name=memo_filename(),
        mime="application/pdf",
        key=button_key,
        help="Everything below, as a paginated memo with source attribution for every figure.",
    )


# --- Report renderers -----------------------------------------------------


def run_risk_analysis(result_state):
    return backend.run_risk_analysis(result_state)


def render_simple(result_state, data_result, button_key):
    row_count = int(value(result_state, "row_count", 0) or 0)
    if row_count == 0:
        st.info("No data was found for that question.")
    else:
        noun = "row" if row_count == 1 else "rows"
        st.markdown(
            f"<div class='status'><strong>{row_count:,} {noun} pulled</strong><br>"
            "<span>Data was retrieved from the loan portfolio.</span></div>",
            unsafe_allow_html=True,
        )
        if not backend.RISK_ANALYSIS_ON_DEMAND:
            # The guard classified this as a data question and the pipeline
            # answered it without scoring. Adding risk analysis now would mean
            # reading the population back out of S3, which only the Fargate
            # tasks are permitted to do -- so say that rather than offer a
            # button that cannot work.
            st.caption(
                "The schema guard judged this a data question, so no risk "
                "analysis was run. Ask it in terms of exposure, loss or "
                "concentration to get a full report."
            )
            return result_state, False
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


def render_risk_report(result_state, turn_key=None, elapsed=None):
    credit = value(result_state, "credit_metrics", {}) or {}
    rate = value(result_state, "rate_metrics", {}) or {}
    compliance = value(result_state, "compliance_result", {}) or {}
    capital = value(compliance, "regulatory_capital", {}) or {}
    data_result = value(result_state, "data_agent_result")
    sql_query = value(data_result, "sql_query") or value(result_state, "sql_query", "")
    loan_count = int(number(credit, "loan_count", value(result_state, "row_count", 0)) or 0)

    st.markdown(
        f"<div class='status'><strong>Risk report generated from {loan_count:,} loans</strong></div>",
        unsafe_allow_html=True,
    )
    render_trace(result_state, elapsed)
    render_memo_download(result_state, f"memo-{turn_key}")
    render_scoring_coverage(credit, int(value(result_state, "row_count", 0) or 0))

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
    render_distribution(value(credit, "risk_tier_distribution", {}),
                        key="tiers-%s" % turn_key)

    st.subheader("Portfolio concentration")
    purpose, region = st.columns(2)
    with purpose:
        render_concentration("By purpose", value(rate, "concentration_by_purpose"),
                             key="purpose-%s" % turn_key)
    with region:
        render_concentration("By region", value(rate, "concentration_by_region"),
                             key="region-%s" % turn_key)

    st.subheader("Interest-rate sensitivity")
    render_interest_rate(value(rate, "repricing_gap"), key="rate-%s" % turn_key)

    st.subheader("Compliance")
    render_compliance_banner(compliance)
    flags = value(compliance, "flags", []) or []
    with st.expander(f"Compliance checks ({len(flags)})", expanded=bool(value(compliance, "any_breach", False))):
        if not flags:
            st.info("No compliance checks were returned.")
        for flag_index, flag in enumerate(flags):
            render_flag(flag, key="flag-%s-%d" % (turn_key, flag_index))
            st.divider()
    citation = value(compliance, "regulatory_capital_citation")
    if citation:
        with st.expander("Regulatory capital methodology"):
            st.info(citation)
    if sql_query:
        with st.expander("SQL query used"):
            st.code(sql_query, language="sql")


def render_response(result_state, turn_key=None, elapsed=None):
    data_result = value(result_state, "data_agent_result")
    data_available = bool(value(result_state, "data_available", False))
    retries = int(value(data_result, "retries_used", 0) or 0)
    success = bool(value(data_result, "success", False))
    pipeline_error = value(result_state, "pipeline_error")
    if pipeline_error:
        # An execution that failed is not the same as a question the data cannot
        # answer, and saying so would misattribute an infrastructure fault to the
        # loan book. The cause is the state machine's own Cause string, which
        # says what stopped and what to do about it.
        st.error(
            f"The analysis pipeline stopped: **{pipeline_error}**. "
            f"{value(data_result, 'message', '')}"
        )
        render_trace(result_state, elapsed)
    elif not data_available and not success and retries == 0:
        reason = value(result_state, "guard_reason") or value(
            data_result, "message", "No matching portfolio data was available."
        )
        st.warning(f"I couldn't answer that from the loan portfolio data: {reason}")
        render_trace(result_state, elapsed)
    elif not data_available and retries > 0:
        st.error(
            "I tried a few times but couldn't retrieve valid data for this question. "
            f"{value(data_result, 'message', '')}"
        )
        render_trace(result_state, elapsed)
    elif not bool(value(result_state, "requires_risk_analysis", True)):
        return render_simple(result_state, data_result, f"risk-analysis-{turn_key}")
    else:
        render_risk_report(result_state, turn_key, elapsed)
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


# --- Sidebar --------------------------------------------------------------


def render_sidebar():
    """Sample questions, portfolio scope, and the source legend.

    The legend earns its place: the single most misleading thing a risk tool can
    do is let an internal risk-appetite number look like a regulatory
    requirement, so the distinction is stated before any report is read.
    """
    with st.sidebar:
        st.markdown("### Try a question")
        for index, question in enumerate(SAMPLE_QUESTIONS):
            if st.button(question, key=f"sample-{index}", width="stretch"):
                st.session_state.pending_query = question
                st.rerun()

        st.divider()
        st.markdown("### Portfolio")
        st.caption(
            "878,317 US unsecured consumer instalment loans issued Oct 2013 - Dec 2018, "
            "joined to borrower credit attributes. Queried read-only."
        )

        st.divider()
        st.markdown("### Reading a compliance flag")
        st.markdown(
            "<span class='pill basel'>Basel III</span> &nbsp; a regulatory floor, quoted from "
            "the primary BIS text.<br><br>"
            "<span class='pill internal'>Internal</span> &nbsp; a risk-appetite ceiling set by "
            "policy. Not a regulatory requirement, and never presented as one.",
            unsafe_allow_html=True,
        )

        st.divider()
        # st.navigation renders the page list at the top of this sidebar, so a
        # second link to the same page would only duplicate it -- the caption
        # explains what is behind that nav entry instead.
        st.markdown("### Methodology & transparency")
        st.caption(
            "Model validation, Basel formulas as implemented, and every assumption this "
            "system makes. Open it from the navigation at the top of this sidebar."
        )
        if st.session_state.get("conversation"):
            if st.button("Clear conversation", width="stretch"):
                st.session_state.conversation = []
                st.rerun()


# --- Script body ----------------------------------------------------------

if "conversation" not in st.session_state:
    st.session_state.conversation = []

render_sidebar()

st.markdown(
    "<div class='brand'><div class='brand-kicker'>Portfolio intelligence</div><h1>RiskForge</h1>"
    "<p>Ask a question about the loan book and receive an auditable risk view.</p></div>",
    unsafe_allow_html=True,
)

for turn_index, turn in enumerate(st.session_state.conversation):
    with st.chat_message("user"):
        st.write(turn["query"])
    with st.chat_message("assistant"):
        updated_result, changed = render_response(turn["result"], turn_index, turn.get("elapsed"))
        if changed:
            # Rerun instead of rendering twice in one pass: the old code left the
            # pre-analysis prompt above a full report in the same message.
            turn["result"] = updated_result
            st.rerun()

# A sidebar sample question is queued for the next run rather than executed
# inside the button callback, so it flows through exactly the same path as typing.
query = st.chat_input("Ask about exposure, expected loss, concentration, or compliance...")
if not query:
    query = st.session_state.pop("pending_query", None)

if query:
    with st.chat_message("user"):
        st.write(query)
    with st.chat_message("assistant"):
        with st.spinner("Running the RiskForge analysis..."):
            started = time.perf_counter()
            result_state = backend.run_query(query)
            elapsed = time.perf_counter() - started
        render_response(result_state, len(st.session_state.conversation), elapsed)
    st.session_state.conversation.append({"query": query, "result": result_state, "elapsed": elapsed})
    scroll_to_latest_message()
