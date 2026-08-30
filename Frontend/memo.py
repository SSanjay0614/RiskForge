"""Risk memo PDF generation.

Renders a completed RiskGraphState into a print-ready "Portfolio Risk
Memorandum": the same figures the web report shows, plus the retrieval trail
and a source-attribution appendix that maps every class of number back to
where it came from (portfolio database, trained model, deterministic formula,
or a specific Basel III paragraph).

Charts are drawn with matplotlib rather than exported from Plotly so the memo
needs no extra dependency beyond reportlab, which is already installed.
"""

from datetime import datetime, timezone
import hashlib
import io
from xml.sax.saxutils import escape

import matplotlib

# Must be selected before pyplot is imported -- there is no display on a
# server, and Streamlit reruns this from a worker thread.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfgen import canvas as pdfcanvas  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from theme import (  # noqa: E402
    drop_reason_label,
    limit_value as _limit_value,
    metric_label,
    money,
    number,
    pct,
    source_label,
    value,
)


# --- Print palette -------------------------------------------------------
# Deliberately not the dark web palette: this renders on white paper.

INK = colors.HexColor("#14201f")
MUTED = colors.HexColor("#5c6b68")
ACCENT = colors.HexColor("#1d6b58")
BREACH = colors.HexColor("#b3402f")
BREACH_SOFT = colors.HexColor("#fbeae7")
LINE = colors.HexColor("#c9d6d2")
ZEBRA = colors.HexColor("#f4f8f7")

MARGIN = 18 * mm
CONTENT_WIDTH = A4[0] - 2 * MARGIN

FOOTER_NOTE = (
    "RiskForge - automatically generated. Basel III citations verified against BIS primary source. "
    "Liability figures are a documented synthetic assumption."
)


# --- Paragraph styles ----------------------------------------------------

TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=INK)
KICKER = ParagraphStyle("kicker", fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=ACCENT, spaceAfter=2)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=8.6, leading=12.2, textColor=INK)
SMALL = ParagraphStyle("small", fontName="Helvetica", fontSize=7.4, leading=10.2, textColor=MUTED)
MONO = ParagraphStyle("mono", fontName="Courier", fontSize=7.4, leading=10.2, textColor=INK)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7.6, leading=10, textColor=INK)
CELL_HEAD = ParagraphStyle("cellhead", fontName="Helvetica-Bold", fontSize=7.6, leading=10, textColor=colors.white)


class NumberedCanvas(pdfcanvas.Canvas):
    """Two-pass canvas so the footer can print 'Page N of M'.

    Pages are buffered on showPage() and only actually written in save(),
    at which point the total count is known.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.4)
        self.line(MARGIN, 15 * mm, A4[0] - MARGIN, 15 * mm)
        self.setFont("Helvetica", 6.4)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, 11 * mm, FOOTER_NOTE)
        self.drawRightString(A4[0] - MARGIN, 11 * mm, f"Page {self._pageNumber} of {total}")
        self.restoreState()


# --- Charts --------------------------------------------------------------

ACCENT_HEX = "#1d6b58"
BREACH_HEX = "#b3402f"
MUTED_HEX = "#5c6b68"
LINE_HEX = "#c9d6d2"
INK_HEX = "#14201f"
TIER_HEX = {"low": "#1d6b58", "medium": "#7aa86a", "high": "#d59a3c", "very high": "#b3402f"}
TIER_ORDER = ["low", "medium", "high", "very high"]


def _style_axes(ax, xlabel=None):
    ax.set_facecolor("white")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(LINE_HEX)
    ax.tick_params(colors=MUTED_HEX, labelsize=7.5, length=2)
    ax.xaxis.label.set_color(MUTED_HEX)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=7.5)
    ax.grid(axis="x", color=LINE_HEX, linewidth=0.5, alpha=0.7)
    ax.set_axisbelow(True)


def _to_flowable(fig, width_mm):
    """Render a matplotlib figure to a platypus Image at a fixed print width."""
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buffer.seek(0)
    width_in, height_in = fig.get_size_inches()
    width = width_mm * mm
    return Image(buffer, width=width, height=width * (height_in / width_in))


def _chart_tiers(distribution):
    if not distribution:
        return None
    items = sorted(
        distribution.items(),
        key=lambda kv: TIER_ORDER.index(str(kv[0]).strip().lower())
        if str(kv[0]).strip().lower() in TIER_ORDER
        else len(TIER_ORDER),
    )
    labels = [str(label).title() for label, _ in items]
    shares = [float(share) * 100 for _, share in items]
    bar_colors = [TIER_HEX.get(str(label).strip().lower(), MUTED_HEX) for label, _ in items]

    fig, ax = plt.subplots(figsize=(6.4, 0.5 * len(labels) + 0.9))
    ax.barh(labels, shares, color=bar_colors, height=0.62)
    ax.invert_yaxis()
    _style_axes(ax, "Portfolio share (%)")
    for y, share in enumerate(shares):
        ax.text(share + max(shares) * 0.015, y, f"{share:.1f}%", va="center", fontsize=7.2, color=MUTED_HEX)
    ax.set_xlim(0, max(shares) * 1.16 if max(shares) else 1)
    return _to_flowable(fig, 120)


def _chart_concentration(result, top_n=10):
    shares = value(result, "segment_shares", {}) or {}
    if not shares:
        return None
    items = sorted(shares.items(), key=lambda kv: float(kv[1]), reverse=True)[:top_n]
    labels = [str(label).replace("_", " ") for label, _ in items]
    values = [float(share) * 100 for _, share in items]

    fig, ax = plt.subplots(figsize=(6.4, 0.32 * len(labels) + 0.9))
    ax.barh(labels, values, color=ACCENT_HEX, height=0.6)
    ax.invert_yaxis()
    _style_axes(ax, "Exposure share (%)")
    ax.set_xlim(0, max(values) * 1.12 if max(values) else 1)
    return _to_flowable(fig, 84)


def _compact_money(amount, _pos=None):
    """Axis tick labels -- raw dollar counts run off the plot at portfolio scale."""
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:.1f}bn"
    if magnitude >= 1e6:
        return f"{sign}${magnitude / 1e6:.1f}m"
    if magnitude >= 1e3:
        return f"{sign}${magnitude / 1e3:.0f}k"
    return f"{sign}${magnitude:.0f}"


def _chart_repricing(result):
    buckets = value(result, "buckets", []) or []
    if not buckets:
        return None
    labels = [value(bucket, "bucket_label", "?") for bucket in buckets]
    gaps = [number(bucket, "gap") for bucket in buckets]
    cumulative = [number(bucket, "cumulative_gap") for bucket in buckets]

    fig, ax = plt.subplots(figsize=(6.4, 2.3))
    ax.bar(labels, gaps, color=[ACCENT_HEX if gap >= 0 else BREACH_HEX for gap in gaps], width=0.56)
    # The periodic gap says what reprices inside each window; the cumulative
    # line says what is repriced by the end of it, which is the one that drives
    # the earnings measure below the chart.
    ax.plot(labels, cumulative, color=INK_HEX, linewidth=1.1, marker="o", markersize=3.2,
            label="Cumulative gap")
    ax.axhline(0, color=MUTED_HEX, linewidth=0.7)
    _style_axes(ax)
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color=LINE_HEX, linewidth=0.5, alpha=0.7)
    ax.set_ylabel("Gap", fontsize=7.5, color=MUTED_HEX)
    ax.yaxis.set_major_formatter(FuncFormatter(_compact_money))
    ax.legend(frameon=False, fontsize=7, labelcolor=MUTED_HEX, loc="best")
    return _to_flowable(fig, 150)


def _chart_shocks(result):
    """Change in the next 12 months of net interest income per rate shock."""
    shocks = value(result, "rate_shocks", []) or []
    if not shocks:
        return None
    labels = [f"{int(number(shock, 'shock_bps')):+d}" for shock in shocks]
    changes = [number(shock, "net_interest_income_change") for shock in shocks]

    fig, ax = plt.subplots(figsize=(6.4, 2.1))
    ax.bar(labels, changes, color=[ACCENT_HEX if change >= 0 else BREACH_HEX for change in changes],
           width=0.5)
    ax.axhline(0, color=MUTED_HEX, linewidth=0.7)
    _style_axes(ax, "Parallel rate shock (bps)")
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color=LINE_HEX, linewidth=0.5, alpha=0.7)
    ax.set_ylabel("Change in 12m NII", fontsize=7.5, color=MUTED_HEX)
    ax.yaxis.set_major_formatter(FuncFormatter(_compact_money))
    return _to_flowable(fig, 150)



# --- Table helpers -------------------------------------------------------


def _table(rows, col_widths, header=True, zebra=True, highlight_rows=(), grid=True):
    def cell(content, style):
        # Flowables (charts, nested tables) pass straight through; anything
        # else is treated as inline markup and wrapped in a Paragraph.
        return content if hasattr(content, "wrap") else Paragraph(str(content), style)

    data = [[cell(content, CELL_HEAD if header and r == 0 else CELL) for content in row]
            for r, row in enumerate(rows)]
    table = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if grid:
        style.append(("GRID", (0, 0), (-1, -1), 0.35, LINE))
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), ACCENT))
    if zebra:
        start = 1 if header else 0
        for row_index in range(start, len(data)):
            if (row_index - start) % 2 == 1:
                style.append(("BACKGROUND", (0, row_index), (-1, row_index), ZEBRA))
    for row_index in highlight_rows:
        style.append(("BACKGROUND", (0, row_index), (-1, row_index), BREACH_SOFT))
        style.append(("LINEBEFORE", (0, row_index), (0, row_index), 2, BREACH))
    table.setStyle(TableStyle(style))
    return table


def _kv_grid(pairs):
    """Four-column label/value grid for the executive summary block."""
    rows = []
    for index in range(0, len(pairs), 2):
        chunk = pairs[index:index + 2]
        row = []
        for label, display in chunk:
            row.extend([f"<b>{label}</b>", display])
        while len(row) < 4:
            row.append("")
        rows.append(row)
    widths = [CONTENT_WIDTH * f for f in (0.29, 0.21, 0.29, 0.21)]
    return _table(rows, widths, header=False, zebra=False)


def _section(title, *flowables):
    return [Paragraph(title, H2), *flowables]


def _esc(text) -> str:
    """Paragraph content is parsed as mini-HTML, so a query containing '&' or
    '<' would otherwise break the build."""
    return escape(str(text if text is not None else ""))


# --- Memo ----------------------------------------------------------------


def build_risk_memo(result_state) -> bytes:
    """Render a completed risk analysis into PDF bytes."""

    credit = value(result_state, "credit_metrics", {}) or {}
    rate = value(result_state, "rate_metrics", {}) or {}
    compliance = value(result_state, "compliance_result", {}) or {}
    capital = value(compliance, "regulatory_capital", {}) or {}
    data_result = value(result_state, "data_agent_result")

    query = str(value(result_state, "query", "") or "")
    sql_query = str(value(data_result, "sql_query") or value(result_state, "sql_query", "") or "")
    row_count = int(number(data_result, "row_count", number(result_state, "row_count", 0)))
    retries = int(number(data_result, "retries_used", 0))
    loan_count = int(number(credit, "loan_count", row_count))
    truncated = bool(value(result_state, "truncated", False))
    flags = value(compliance, "flags", []) or []
    any_breach = bool(value(compliance, "any_breach", False))
    repricing = value(rate, "repricing_gap")

    generated = datetime.now(timezone.utc)
    memo_id = hashlib.sha256(
        f"{query}|{sql_query}|{generated.isoformat()}".encode("utf-8")
    ).hexdigest()[:10].upper()

    story = []

    # 1. Header
    story.append(Paragraph("RISKFORGE&nbsp;&nbsp;|&nbsp;&nbsp;PORTFOLIO INTELLIGENCE", KICKER))
    story.append(Paragraph("Portfolio Risk Memorandum", TITLE))
    story.append(Spacer(1, 3 * mm))
    story.append(_table([
        ["Memo reference", memo_id, "Generated", generated.strftime("%Y-%m-%d %H:%M UTC")],
        ["Population", f"{loan_count:,} loans scored", "Basis", "Loan-level, exposure-weighted"],
    ], [CONTENT_WIDTH * f for f in (0.20, 0.30, 0.18, 0.32)], header=False, zebra=False))
    story.append(Spacer(1, 2 * mm))

    # 2. Request and retrieval
    # rows_retrieved is what SQL returned; loan_count is what the PD/LGD models
    # could actually score. Reporting both, with the reason for any difference,
    # keeps the memo from showing two population figures that silently disagree.
    rows_retrieved = int(number(credit, "rows_retrieved", row_count))
    dropped_counts = value(credit, "dropped_reason_counts", {}) or {}
    excluded = max(rows_retrieved - loan_count, 0)

    retrieval_block = [
        Paragraph(
            f"Question as asked: <b>&ldquo;{_esc(query)}&rdquo;</b>" if query else "Question unavailable.",
            BODY,
        ),
        Spacer(1, 2 * mm),
        _table([
            ["Rows retrieved", f"{rows_retrieved:,}", "Retrieval retries used", str(retries)],
            ["Loans scored", f"{loan_count:,}", "Result truncated", "Yes" if truncated else "No"],
        ], [CONTENT_WIDTH * f for f in (0.22, 0.20, 0.34, 0.24)], header=False, zebra=False),
    ]
    if excluded:
        reasons = ", ".join(
            f"{drop_reason_label(reason).lower()} ({int(count):,})"
            for reason, count in sorted(dropped_counts.items(), key=lambda kv: -float(kv[1]))
            if float(count) > 0
        )
        retrieval_block += [
            Spacer(1, 2 * mm),
            Paragraph(
                f"<b>{excluded:,} of {rows_retrieved:,} retrieved loans "
                f"({excluded / rows_retrieved * 100:.2f}%) were not scored.</b> The PD and LGD models "
                "require a complete feature row and these loans are missing at least one input"
                f"{f': {_esc(reasons)}' if reasons else ''}. They are excluded rather than scored on an "
                "imputed value, so exposure, expected loss and regulatory capital in this memo are "
                "totals over the scored population. Concentration and the interest-rate view use all "
                "retrieved rows, since neither depends on a model score.",
                SMALL,
            ),
        ]
    retrieval_block += [
        Spacer(1, 2 * mm),
        Paragraph("SQL executed against the portfolio database:", SMALL),
        Spacer(1, 1 * mm),
        _table([[Paragraph(_esc(sql_query) or "unavailable", MONO)]], [CONTENT_WIDTH], header=False, zebra=False),
    ]
    story += _section("1. Request and retrieval", *retrieval_block)

    # 3. Executive summary
    verdict = (
        "One or more limits are breached - see section 7."
        if any_breach
        else "All checked limits are within tolerance - see section 7."
    )
    story += _section(
        "2. Executive summary",
        _kv_grid([
            ("Total exposure (EAD)", money(number(credit, "total_exposure"))),
            ("Expected loss", money(number(credit, "total_expected_loss"))),
            ("Exposure-weighted PD", pct(number(credit, "exposure_weighted_avg_pd"))),
            ("Exposure-weighted LGD", pct(number(credit, "exposure_weighted_avg_lgd"))),
            ("Expected loss rate", pct(number(credit, "expected_loss_rate"))),
            ("Average risk weight", f"{number(capital, 'avg_risk_weight_pct'):.2f}%"),
            ("Total RWA", money(number(capital, "total_rwa"))),
            ("Capital reserve (8% of RWA)", money(number(capital, "total_capital_requirement_8pct"))),
        ]),
        Spacer(1, 2 * mm),
        Paragraph(verdict, BODY),
    )

    # 4. Credit risk
    credit_block = [
        Paragraph(
            "Expected loss is computed per loan as EL = PD x LGD x EAD and aggregated "
            "exposure-weighted, so a large exposure carries proportionally more weight than a "
            "small one. PD comes from a calibrated behavioural model scoring loans already on "
            "the book; LGD from a regression trained on realised recoveries, with loss measured "
            "against exposure at default rather than original loan size.",
            BODY,
        ),
        Spacer(1, 2 * mm),
        _table([
            ["Metric", "Value", "Origin"],
            ["Exposure-weighted PD", pct(number(credit, "exposure_weighted_avg_pd")), "pd_model_calibrated.joblib"],
            ["Exposure-weighted LGD", pct(number(credit, "exposure_weighted_avg_lgd")), "lgd_model.joblib"],
            ["Total exposure (EAD)", money(number(credit, "total_exposure")), "Outstanding principal, portfolio database"],
            ["Total expected loss", money(number(credit, "total_expected_loss")), "EL = PD x LGD x EAD"],
            ["Expected loss rate", pct(number(credit, "expected_loss_rate")), "Total EL / total EAD"],
        ], [CONTENT_WIDTH * f for f in (0.30, 0.20, 0.50)]),
    ]
    tier_chart = _chart_tiers(value(credit, "risk_tier_distribution", {}) or {})
    if tier_chart is not None:
        credit_block += [Spacer(1, 3 * mm), Paragraph("Risk tier distribution", SMALL), tier_chart]
    story += _section("3. Credit risk", *credit_block)

    # 5. Concentration and interest rate risk
    story.append(PageBreak())
    conc_rows = [["Dimension", "HHI (0-10000)", "Assessment"]]
    for label, key in (("Sector (loan purpose)", "concentration_by_purpose"), ("Region (borrower state)", "concentration_by_region")):
        result = value(rate, key)
        conc_rows.append([
            label,
            f"{number(result, 'hhi_score_10000_scale'):,.0f}" if result else "unavailable",
            _esc(value(result, "diversification_level", "unavailable")) if result else "unavailable",
        ])
    conc_block = [
        Paragraph(
            "Concentration is measured as the Herfindahl-Hirschman Index over exposure shares, "
            "reported on the conventional 0-10000 scale. Above 2500 is treated as highly "
            "concentrated, following the US DOJ merger-guideline convention.",
            BODY,
        ),
        Spacer(1, 2 * mm),
        _table(conc_rows, [CONTENT_WIDTH * f for f in (0.38, 0.22, 0.40)]),
    ]
    charts = [(_chart_concentration(value(rate, "concentration_by_purpose")), "By purpose"),
              (_chart_concentration(value(rate, "concentration_by_region")), "By region")]
    charts = [(chart, caption) for chart, caption in charts if chart is not None]
    if charts:
        conc_block.append(Spacer(1, 3 * mm))
        conc_block.append(_table(
            [[Paragraph(caption, SMALL) for _, caption in charts], [chart for chart, _ in charts]],
            [CONTENT_WIDTH / len(charts)] * len(charts), header=False, zebra=False, grid=False,
        ))
    story += _section("4. Concentration risk", *conc_block)

    # 6. Interest rate risk
    rate_block = []
    if repricing:
        earnings_available = bool(value(repricing, "earnings_view_available", False))
        as_of = str(value(repricing, "as_of_date", "") or "")
        # Both deposit-side fields default to 0.0 on the result model, so a zero
        # means "not reported by this result", not a zero-rate, zero-funded book.
        # Never print a zero as though it were the documented assumption.
        funding_ratio = number(repricing, "deposit_funding_ratio")
        pass_through = number(repricing, "deposit_rate_pass_through")
        assumptions_reported = funding_ratio > 0.0 and pass_through > 0.0

        rate_block.append(Paragraph(
            "Repricing gap per bucket is rate-sensitive assets minus rate-sensitive liabilities. "
            "Assets are bucketed by months remaining on each fixed-rate term loan, which is when "
            "a fixed loan effectively reprices. Buckets are measured from the portfolio's own "
            f"latest issue date{f' ({_esc(as_of)})' if as_of else ''}, not the date this memo was "
            "generated, so a historical book is not reported as fully matured.",
            BODY,
        ))

        if earnings_available:
            rate_block += [
                Spacer(1, 2 * mm),
                _kv_grid([
                    ("Net interest income (annual)", money(number(repricing, "net_interest_income_annual"))),
                    ("Net interest margin", pct(number(repricing, "net_interest_margin"))),
                    ("Earnings at risk (12m)", money(number(repricing, "earnings_at_risk_12m"))),
                    ("Loan-to-deposit ratio", f"{number(repricing, 'loan_to_deposit_ratio'):.4f}"),
                    ("Portfolio yield (observed)", pct(number(repricing, "portfolio_yield"))),
                    ("Deposit rate (assumed)", pct(number(repricing, "deposit_rate"))),
                ]),
                Spacer(1, 2 * mm),
                Paragraph(
                    f"The book earns a weighted-average contractual rate of "
                    f"<b>{pct(number(repricing, 'portfolio_yield'))}</b> on "
                    f"{money(number(repricing, 'total_rate_sensitive_assets'))} of loans, which is "
                    f"{money(number(repricing, 'interest_income_annual'))} of annual interest income. "
                    + (
                        f"Depositors are paid {pct(pass_through)} of that rate - "
                        f"<b>{pct(number(repricing, 'deposit_rate'))}</b> - on a deposit book sized at "
                        f"{funding_ratio:.2f}x the loans, costing "
                        if assumptions_reported
                        else f"Depositors are paid <b>{pct(number(repricing, 'deposit_rate'))}</b> on "
                        f"{money(number(repricing, 'total_rate_sensitive_liabilities'))} of modelled "
                        "deposits, costing "
                    )
                    + f"{money(number(repricing, 'interest_expense_annual'))}. Net interest income is "
                    f"the difference: {money(number(repricing, 'net_interest_income_annual'))}, or "
                    f"{pct(number(repricing, 'net_interest_margin'))} of rate-sensitive assets.",
                    BODY,
                ),
                Spacer(1, 2 * mm),
                Paragraph(
                    "The book is <b>liability-sensitive</b> inside 12 months: more liabilities than "
                    "assets reprice in that window, so rising rates compress net interest income "
                    "before the loan book catches up."
                    if value(repricing, "is_liability_sensitive", False)
                    else "The book is <b>asset-sensitive</b> inside 12 months: more assets than "
                    "liabilities reprice in that window, so rising rates lift net interest income.",
                    BODY,
                ),
            ]

        bucket_rows = [["Bucket", "Rate-sensitive assets", "Rate-sensitive liabilities", "Gap", "Cumulative gap"]]
        for bucket in value(repricing, "buckets", []) or []:
            bucket_rows.append([
                _esc(value(bucket, "bucket_label", "?")),
                money(number(bucket, "rate_sensitive_assets")),
                money(number(bucket, "rate_sensitive_liabilities")),
                money(number(bucket, "gap")),
                money(number(bucket, "cumulative_gap")),
            ])
        bucket_rows.append([
            "<b>Total</b>",
            f"<b>{money(number(repricing, 'total_rate_sensitive_assets'))}</b>",
            f"<b>{money(number(repricing, 'total_rate_sensitive_liabilities'))}</b>",
            f"<b>{money(number(repricing, 'net_gap'))}</b>",
            "",
        ])
        rate_block += [
            Spacer(1, 3 * mm),
            _table(bucket_rows, [CONTENT_WIDTH * f for f in (0.14, 0.22, 0.22, 0.21, 0.21)]),
        ]

        shock_rows = value(repricing, "rate_shocks", []) or []
        if shock_rows:
            shock_table = [["Parallel shock", "Change in 12m NII", "12m NII after", "Change (%)"]]
            for shock in shock_rows:
                shock_table.append([
                    f"{int(number(shock, 'shock_bps')):+d} bps",
                    money(number(shock, "net_interest_income_change")),
                    money(number(shock, "net_interest_income_after")),
                    f"{number(shock, 'pct_change') * 100:+.2f}%",
                ])
            rate_block += [
                Spacer(1, 3 * mm),
                Paragraph(
                    "Earnings at risk under parallel shifts in the rate curve. Each bucket's gap is "
                    "exposed to the shock only for the part of the next 12 months that follows its "
                    "repricing, so the near buckets are weighted by their midpoint (0-3mo at 0.875 of "
                    "the year, 3-12mo at 0.375) and anything repricing beyond 12 months contributes "
                    "nothing. The headline earnings-at-risk figure is the worst of these.",
                    SMALL,
                ),
                Spacer(1, 1.5 * mm),
                _table(shock_table, [CONTENT_WIDTH * f for f in (0.22, 0.26, 0.26, 0.26)]),
            ]

        if value(repricing, "liabilities_are_synthetic", False):
            rate_block += [Spacer(1, 2 * mm), Paragraph(
                "<b>Documented assumption.</b> This portfolio has no liability data - it is a lending "
                "book, not a full bank balance sheet. The asset side is real throughout: balances, "
                "contractual rates, terms and issue dates all come from the retrieved rows. The "
                "deposit side is modelled: "
                + (
                    f"a book sized at {funding_ratio:.2f}x the loans, spread across the four "
                    "buckets at 55% / 25% / 15% / 5% to reflect a retail profile weighted toward "
                    "short-term repricing, and paid "
                    f"{pct(pass_through)} of the rate earned on the loans. "
                    "That pass-through gives a deposit rate near the sub-1% US retail savings rates "
                    "actually observed over this portfolio's 2013-2018 origination window. "
                    if assumptions_reported
                    else f"{money(number(repricing, 'total_rate_sensitive_liabilities'))} of deposits "
                    "spread across the four buckets at 55% / 25% / 15% / 5% to reflect a retail "
                    "profile weighted toward short-term repricing. This analysis did not report the "
                    "funding ratio or the deposit-rate pass-through behind those figures, so they "
                    "are not restated here. "
                )
                + "The gap table, the earnings "
                "figures and the loan-to-deposit limit check all inherit that assumption and should be "
                "read as illustrative rather than as this bank's real funding position.",
                SMALL,
            )]

        for chart in (_chart_shocks(repricing), _chart_repricing(repricing)):
            if chart is not None:
                rate_block += [Spacer(1, 3 * mm), chart]
    else:
        rate_block.append(Paragraph("Repricing gap was not computed for this population.", BODY))
    story += _section("5. Interest rate risk", *rate_block)

    # 7. Regulatory capital
    # No forced page break here: the interest-rate charts above are tall enough
    # to spill onto their own page, and breaking as well left half a page blank.
    if capital:
        capital_block = [
            _table([
                ["Quantity", "Value"],
                ["Exposure at default (EAD)", money(number(capital, "total_ead"))],
                ["Exposure-weighted correlation (R)", f"{number(capital, 'exposure_weighted_avg_correlation'):.4f}"],
                ["Exposure-weighted capital requirement (K)", f"{number(capital, 'exposure_weighted_avg_k'):.4f}"],
                ["Average risk weight", f"{number(capital, 'avg_risk_weight_pct'):.2f}%"],
                ["Risk-weighted assets (RWA)", money(number(capital, "total_rwa"))],
                ["Capital reserve at 8% of RWA", money(number(capital, "total_capital_requirement_8pct"))],
                ["Exposures included", f"{int(number(capital, 'loan_count')):,}"],
            ], [CONTENT_WIDTH * 0.58, CONTENT_WIDTH * 0.42]),
            Spacer(1, 2 * mm),
            Paragraph("Formula as implemented, verified against the primary BIS source:", SMALL),
            _table([[Paragraph(
                "R = 0.03 x (1 - e^(-35 x PD)) / (1 - e^-35) + 0.16 x [1 - (1 - e^(-35 x PD)) / (1 - e^-35)]<br/>"
                "K = LGD x N[(G(PD) + sqrt(R / (1 - R)) x G(0.999)) / sqrt(1 - R)] - PD x LGD<br/>"
                "RWA = K x 12.5 x EAD",
                MONO)]], [CONTENT_WIDTH], header=False, zebra=False),
            Spacer(1, 2 * mm),
            Paragraph(
                "Basel III CRE31.16, IRB risk-weight function for the <b>Other Retail</b> exposure class - "
                "the correct class for unsecured personal instalment loans (not mortgages, not qualifying "
                "revolving). Retail exposures carry no maturity adjustment: maturity is subsumed in the "
                "correlation assumption, per the BIS QIS3 FAQ. Input floors of 0.05% on PD and 30% on LGD "
                "are applied per CRE32.58 before the formula is evaluated. Where K evaluates negative it is "
                "clipped to zero, meaning the exposure attracts no regulatory capital.",
                SMALL,
            ),
        ]
    else:
        capital_block = [Paragraph("Regulatory capital was not computed for this population.", BODY)]
    story += _section("6. Regulatory capital (Basel III IRB)", *capital_block)

    # 8. Compliance findings
    if flags:
        flag_rows = [["Limit", "Actual", "Threshold", "Test", "Source", "Status"]]
        breached_rows = []
        for flag in flags:
            direction = value(flag, "direction", "")
            is_breached = bool(value(flag, "breached", False))
            if is_breached:
                breached_rows.append(len(flag_rows))
            flag_rows.append([
                _esc(metric_label(value(flag, "metric_name", ""))),
                _limit_value(number(flag, "value")),
                _limit_value(number(flag, "threshold")),
                "Ceiling" if direction == "max" else "Floor",
                _esc(source_label(value(flag, "source", ""))),
                "BREACHED" if is_breached else "Within limit",
            ])
        compliance_block = [_table(
            flag_rows,
            [CONTENT_WIDTH * f for f in (0.30, 0.13, 0.13, 0.11, 0.14, 0.19)],
            highlight_rows=breached_rows,
        )]

        cited = [flag for flag in flags if value(flag, "citation")]
        if cited:
            compliance_block += [Spacer(1, 3 * mm), Paragraph("Attribution for each finding:", SMALL)]
            for flag in cited:
                compliance_block += [
                    Spacer(1, 1.5 * mm),
                    Paragraph(
                        f"<b>{_esc(metric_label(value(flag, 'metric_name', '')))}</b> - "
                        f"{_esc(value(flag, 'citation'))}",
                        SMALL,
                    ),
                ]
        compliance_block += [
            Spacer(1, 3 * mm),
            Paragraph(
                "Limit checking is fully deterministic - no model judgement is involved at this step. "
                "Basel-sourced floors carry the exact source paragraph they were taken from; internal "
                "risk-appetite limits are labelled as internal policy rather than being given a "
                "regulatory citation they do not have.",
                SMALL,
            ),
        ]
    else:
        compliance_block = [Paragraph("No compliance checks were returned for this population.", BODY)]
    story += _section("7. Compliance findings", *compliance_block)

    # 9. Source attribution appendix
    provenance = [
        ["Figure", "Class of source", "Attribution"],
        ["Loan and borrower attributes", "Portfolio data",
         f"SQLite portfolio database, Loans joined to Borrowers; {rows_retrieved:,} rows returned by the SQL in section 1"],
        ["Exposure at default (EAD)", "Portfolio data", "Each loan's current outstanding principal"],
        ["Probability of default (PD)", "Trained model",
         "pd_model_calibrated.joblib - XGBoost with isotonic calibration, leakage-audited, behavioural "
         "(scores loans already on the book, not new applicants)"],
        ["Loss given default (LGD)", "Trained model",
         "lgd_model.joblib - XGBoost regression on realised recoveries, loss measured against exposure at default"],
        ["Expected loss", "Deterministic formula", "EL = PD x LGD x EAD, aggregated exposure-weighted"],
        ["Concentration (HHI)", "Deterministic formula",
         "Sum of squared exposure shares on the 0-10000 scale; 2500 threshold per US DOJ merger-guideline convention"],
        ["Repricing gap - asset side", "Portfolio data", "Months remaining on each fixed-rate term loan, "
         "measured from the portfolio's own latest issue date"],
        ["Portfolio yield, interest income", "Portfolio data",
         "Weighted-average contractual int_rate on the retrieved loans - observed, not assumed"],
        ["Repricing gap - liability side", "Documented assumption",
         "Synthetic retail deposit profile (55/25/15/5) sized at 1.05x the loan book - no liability "
         "data exists in this portfolio"],
        ["Deposit rate, interest expense, NII, NIM", "Documented assumption",
         "Depositors paid 5% of the observed asset yield; every figure downstream of that inherits the "
         "assumption"],
        ["Earnings at risk (12m)", "Deterministic formula",
         "Worst change in 12m net interest income across +/-100 and +/-200bp parallel shocks, gaps "
         "weighted by midpoint exposure within the year"],
        ["Loan-to-deposit ratio", "Internal policy",
         "Loans / assumed deposits, checked against an internal 1.1 ceiling - Basel addresses funding "
         "stability through the NSFR, not an LTD cap"],
        ["Correlation R, capital requirement K, RWA", "Regulatory source",
         "Basel III CRE31.16, IRB risk-weight function for Other Retail exposures"],
        ["PD and LGD input floors", "Regulatory source",
         "Basel III CRE32.58 - PD floor 0.05%, LGD floor 30% for unsecured other retail"],
        ["Expected loss rate and HHI thresholds", "Internal policy",
         "Risk_Limits table - illustrative internal risk-appetite defaults, not regulatory requirements"],
    ]
    story += _section(
        "8. Source attribution",
        Paragraph(
            "Every figure in this memo traces to exactly one of four origins. Nothing below is an "
            "unattributed estimate.",
            BODY,
        ),
        Spacer(1, 2 * mm),
        _table(provenance, [CONTENT_WIDTH * f for f in (0.29, 0.18, 0.53)]),
        Spacer(1, 3 * mm),
        Paragraph(
            "Scope note. The underlying portfolio is US unsecured consumer instalment loans originated "
            "between October 2013 and December 2018. The PD model is behavioural and supports "
            "portfolio-level reporting, not individual accept or reject decisions. This memo is "
            "generated automatically from a single query and is not a substitute for a reviewed "
            "credit opinion.",
            SMALL,
        ),
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=22 * mm,
        title=f"RiskForge Risk Memorandum {memo_id}",
        author="RiskForge",
        subject="Loan portfolio risk memorandum",
    )
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer.getvalue()


def memo_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return f"RiskForge_Risk_Memo_{stamp}.pdf"
