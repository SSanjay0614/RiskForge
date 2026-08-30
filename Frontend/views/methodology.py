"""Methodology & Transparency -- the "show your working" page.

Everything a reviewer would ask after reading a risk report: where the numbers
come from, how the models were validated, which figures are assumptions, and
what the system deliberately does not claim. Read-only: this page never invokes
the workflow and never touches a loan-level row.

Declared as a page by Frontend/app.py, which owns set_page_config and the
stylesheet.
"""

import sqlite3
import sys
from pathlib import Path

import streamlit as st

# Streamlit puts the entrypoint's folder on sys.path; this keeps the module
# importable when it is run or imported directly too.
FRONTEND_DIR = Path(__file__).resolve().parent.parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

# theme also bootstraps sys.path so the backend packages import cleanly.
from theme import ARCHITECTURE_DIAGRAM, PROJECT_ROOT, metric_label, source_label  # noqa: E402

# The architecture diagram is a 1024x1536 portrait image. Stretched to the width
# of a wide-layout container it renders about 1650px tall, which is what made the
# page unreadable; a fixed width keeps it in proportion, and Streamlit's own
# fullscreen control is there for anyone who wants to read the fine print.
DIAGRAM_WIDTH_PX = 560


# --- Live portfolio facts -------------------------------------------------
# Read straight from the same database the agents query, so the numbers on this
# page cannot drift from the actual portfolio. Aggregates only -- COUNT, MIN,
# MAX and the limits table. No loan or borrower row is ever selected.

@st.cache_data(show_spinner=False)
def portfolio_facts() -> dict:
    db_path = PROJECT_ROOT / "Database" / "credit_risk.db"
    if not db_path.exists():
        return {"available": False}
    facts = {"available": True}
    # Read-only URI: the page physically cannot write, regardless of the SQL.
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        cursor = connection.cursor()
        facts["loans"] = cursor.execute("SELECT COUNT(*) FROM Loans").fetchone()[0]
        facts["borrowers"] = cursor.execute("SELECT COUNT(*) FROM Borrowers").fetchone()[0]
        facts["loan_columns"] = len(cursor.execute("PRAGMA table_info(Loans)").fetchall())
        facts["borrower_columns"] = len(cursor.execute("PRAGMA table_info(Borrowers)").fetchall())
        facts["first_issue"], facts["last_issue"] = cursor.execute(
            "SELECT MIN(issue_date), MAX(issue_date) FROM Loans"
        ).fetchone()
        facts["limits"] = cursor.execute(
            "SELECT metric_name, threshold, source FROM Risk_Limits ORDER BY source, metric_name"
        ).fetchall()
    return facts


@st.cache_data(show_spinner=False)
def workflow_dot() -> str:
    """DOT for the live LangGraph topology.

    Built from the compiled graph so the diagram cannot describe a pipeline the
    code no longer has. The import pulls in xgboost and the model artifacts and
    costs ~35s cold, hence the cache and the static fallback -- a slow diagram
    should never be the reason the page fails to load.
    """
    try:
        from workflow.graph import graph

        drawable = graph.get_graph()
        nodes = list(drawable.nodes)
        edges = [
            (edge.source, edge.target, bool(getattr(edge, "conditional", False)))
            for edge in drawable.edges
        ]
    except Exception:
        return STATIC_WORKFLOW_DOT
    return _render_dot(nodes, edges)


# Colour by role rather than giving every box the same fill: retrieval, the two
# parallel risk agents, and the terminal states read differently at a glance.
NODE_STYLES = {
    "__start__": ("#55d49b", "#0c1414"),
    "__end__": ("#55d49b", "#0c1414"),
    "guard": ("#3fb8ad", "#0c1414"),
    "generate_sql": ("#3fb8ad", "#0c1414"),
    "execute_sql": ("#3fb8ad", "#0c1414"),
    "evaluate": ("#3fb8ad", "#0c1414"),
    "give_up": ("#ff7564", "#0c1414"),
    "fan_out": ("#9fd67f", "#0c1414"),
    "credit_risk": ("#e6b45c", "#0c1414"),
    "interest_rate_concentration": ("#e6b45c", "#0c1414"),
    "compliance": ("#e6b45c", "#0c1414"),
}

NODE_LABELS = {
    "__start__": "start",
    "__end__": "end",
    "guard": "schema guard",
    "generate_sql": "generate SQL",
    "execute_sql": "execute SQL",
    "evaluate": "evaluate result",
    "give_up": "give up",
    "fan_out": "fan out",
    "credit_risk": "credit risk agent",
    # A real newline would terminate the DOT statement; \n is the label escape.
    "interest_rate_concentration": "interest rate &\\nconcentration agent",
    "compliance": "compliance agent",
}


def _render_dot(nodes, edges) -> str:
    lines = [
        "digraph RiskForge {",
        # size caps the rendered SVG at 6.2in (~446px) on its longest side.
        # Without it the client renderer scaled an eleven-node top-to-bottom
        # graph to the full container width, which made it enormous.
        '  rankdir=TB; size="6.2,6.2"; bgcolor="transparent"; splines=spline;',
        "  nodesep=0.3; ranksep=0.38;",
        '  node [shape=box style="rounded,filled" fontname="DM Sans" fontsize=11 penwidth=0];',
        '  edge [color="#8fada7" fontname="DM Sans" fontsize=9 arrowsize=0.7];',
    ]
    for node in nodes:
        fill, font = NODE_STYLES.get(node, ("#3fb8ad", "#0c1414"))
        label = NODE_LABELS.get(node, str(node).replace("_", " "))
        lines.append(f'  "{node}" [label="{label}" fillcolor="{fill}" fontcolor="{font}"];')
    for source, target, conditional in edges:
        # Dashed = the router decided; solid = the edge always fires. Deliberately
        # unlabelled: four identical "conditional" tags on the evaluate branches
        # add clutter without adding information the legend does not already give.
        style = " [style=dashed]" if conditional else ""
        lines.append(f'  "{source}" -> "{target}"{style};')
    lines.append("}")
    return "\n".join(lines)


STATIC_WORKFLOW_DOT = _render_dot(
    list(NODE_LABELS),
    [
        ("__start__", "guard", False),
        ("guard", "generate_sql", True),
        ("guard", "__end__", True),
        ("generate_sql", "execute_sql", False),
        ("execute_sql", "evaluate", False),
        ("evaluate", "generate_sql", True),
        ("evaluate", "give_up", True),
        ("evaluate", "fan_out", True),
        ("evaluate", "__end__", True),
        ("give_up", "__end__", False),
        ("fan_out", "credit_risk", False),
        ("fan_out", "interest_rate_concentration", False),
        ("credit_risk", "compliance", False),
        ("interest_rate_concentration", "compliance", False),
        ("compliance", "__end__", False),
    ],
)


# --- Page ----------------------------------------------------------------

st.markdown(
    "<div class='brand'><div class='brand-kicker'>Methodology &amp; transparency</div>"
    "<h1>How RiskForge produces a number</h1>"
    "<p>Every figure in a risk report traces to portfolio data, a trained model, a "
    "deterministic formula, a cited regulation, or a documented assumption.</p></div>",
    unsafe_allow_html=True,
)

st.subheader("1. Overview")
st.markdown(
    "<p class='method-lead'>A question in plain English is checked against the database "
    "schema, translated into SQL, executed read-only, and then judged by the system "
    "itself: if the returned rows do not actually answer the question, the query is "
    "rewritten with that feedback and retried, up to three times. Only when the data is "
    "accepted <em>and</em> the question genuinely needs risk analysis do the credit risk "
    "and the interest-rate/concentration agents run -- in parallel, because neither "
    "depends on the other. Their outputs converge on the compliance agent, which checks "
    "the computed metrics against internal risk-appetite limits and Basel III floors and "
    "calculates regulatory capital. A simple count returns immediately instead of being "
    "buried under an irrelevant report.</p>",
    unsafe_allow_html=True,
)

st.subheader("2. System architecture")
if ARCHITECTURE_DIAGRAM.exists():
    st.image(str(ARCHITECTURE_DIAGRAM), width=DIAGRAM_WIDTH_PX)
    st.caption("Use the expand control on the image to view it full size.")
else:
    st.markdown(
        "<div class='method-note'>Architecture diagram not found at "
        f"<code>{ARCHITECTURE_DIAGRAM.name}</code>.</div>",
        unsafe_allow_html=True,
    )
st.caption(
    "The interface talks only to the compiled LangGraph workflow. The workflow owns the "
    "database connection, the model artifacts and the regulatory logic; the LLM is used "
    "for SQL generation and result evaluation, never for arithmetic."
)

st.markdown("**Live workflow topology**")
st.graphviz_chart(workflow_dot(), width="content")
st.caption(
    "Rendered from the compiled graph at runtime, not drawn by hand -- dashed edges are "
    "routed by a condition, solid edges always fire. The two amber agents downstream of "
    "fan_out execute concurrently and fan back in on compliance."
)

st.subheader("3. Data layer")
facts = portfolio_facts()
if not facts.get("available"):
    st.markdown(
        "<div class='method-note'>The portfolio database is not present in this "
        "environment, so the live counts below cannot be read. Run "
        "<code>python -m Database.seed_db</code> to build it.</div>",
        unsafe_allow_html=True,
    )
else:
    data_cards = st.columns(4)
    data_cards[0].metric("Loans", f"{facts['loans']:,}")
    data_cards[1].metric("Borrower records", f"{facts['borrowers']:,}")
    data_cards[2].metric(
        "Columns", f"{facts['loan_columns']} + {facts['borrower_columns']}",
        help="Loans table columns + Borrowers table columns.",
    )
    data_cards[3].metric(
        "Issue dates", f"{str(facts['first_issue'])[:7]} to {str(facts['last_issue'])[:7]}"
    )
    st.caption(
        "Read live from the same SQLite portfolio the agents query, over a read-only "
        "connection, using aggregates only. Two tables: Loans (contract terms, balances, "
        "status) joined to Borrowers (credit and income attributes) on `loan_id`."
    )
    limits = facts.get("limits") or []
    if limits:
        st.markdown(f"**Risk limits in force ({len(limits)})**")
        st.table(
            {
                "Limit": [metric_label(row[0]) for row in limits],
                "Threshold": [
                    f"{row[1]:,.4f}" if abs(row[1]) < 1 else f"{row[1]:,.0f}" for row in limits
                ],
                "Source": [source_label(row[2]) for row in limits],
            }
        )
        st.caption(
            "Basel-sourced rows carry the exact paragraph the threshold came from. Internal "
            "rows are risk-appetite settings and are labelled as such rather than being given "
            "a regulatory citation they do not have."
        )

st.subheader("4. Models")
st.markdown(
    "<p class='method-lead'>Two trained models, both scoring loans that are already on the "
    "book -- this is behavioural risk assessment, not new-applicant underwriting.</p>",
    unsafe_allow_html=True,
)

with st.expander("PD -- probability of default (and the leakage investigation)", expanded=True):
    st.markdown(
        "XGBoost classifier wrapped in isotonic `CalibratedClassifierCV`, so the output is a "
        "usable probability rather than a ranking score -- expected loss multiplies PD "
        "directly, so a miscalibrated PD corrupts every downstream figure.\n\n"
        "**The number that mattered was the one that got worse.** The first version scored "
        "roughly 0.96 AUC. That is not a good credit model; that is a leak. Tracing it back, "
        "the feature set contained fields only populated *after* the outcome was known -- "
        "post-origination payment and recovery information that encodes the label. Removing "
        "every such field dropped AUC to **0.729**, which sits inside the **0.65-0.75** band "
        "typically published for behavioural consumer-credit scorecards. The lower number is "
        "the honest one and it is the model in use."
    )
    st.markdown(
        "Six candidates were compared (logistic regression, random forest, Optuna-tuned "
        "XGBoost, LightGBM, CatBoost and a stacking ensemble) and selected on a combined "
        "**80% ROC-AUC / 20% Brier score** criterion rather than raw AUC, because a "
        "well-ranked but badly calibrated PD still produces a wrong expected loss. Final "
        "Brier score **0.143**."
    )
    st.markdown(
        "<div class='method-note'>Risk tiers (Low / Medium / High / Very High) are cut on "
        "calibrated PD using thresholds fitted at training time and stored alongside the "
        "model, so tier boundaries do not shift between runs.</div>",
        unsafe_allow_html=True,
    )

with st.expander("LGD -- loss given default"):
    st.markdown(
        "XGBoost regressor trained on realised recoveries, with the target defined as loss "
        "**relative to exposure at default** rather than to the original loan amount. That "
        "distinction is the standard definition and it matters: a loan that has amortised for "
        "three years has far less at risk than its original size implies, and measuring "
        "against the original amount understates severity.\n\n"
        "Test **R^2 is 0.235**. Recovery outcomes are genuinely noisy -- published LGD models "
        "on unsecured consumer portfolios report R^2 broadly in the **4-43%** range -- so this "
        "is a mid-range result presented as-is rather than tuned until it flattered."
    )

with st.expander("Train/serve consistency"):
    st.markdown(
        "Retrieved rows pass through the *same* feature-engineering code path used to build "
        "the training matrix, and the model's stored feature-name list is used to align "
        "columns before scoring. Categorical encodings are reproduced from persisted maps "
        "rather than re-derived from whatever rows happen to be in the current query -- "
        "otherwise identical loans would score differently depending on the filter applied."
    )

st.subheader("5. Agent workflow")
st.markdown(
    "<p class='method-lead'>Four agents, each with a narrow remit. The orchestration is a "
    "real graph, not a linear script dressed up as one.</p>",
    unsafe_allow_html=True,
)

with st.expander("Data agent -- agentic retrieval", expanded=True):
    st.markdown(
        "- Translates the question into SQL against the live portfolio\n"
        "- A schema-relevance guard short-circuits questions the data genuinely cannot "
        "answer, instead of generating confident SQL for a column that does not exist\n"
        "- Evaluates its own retrieved rows and retries with that feedback on a bounded "
        "loop -- it neither fails silently nor loops forever\n"
        "- Enforces read-only database access at the connection level, not merely by asking "
        "the model nicely, plus a row-count ceiling against pathological queries\n"
        "- Classifies lookup/count questions separately from risk questions, so a simple "
        "answer stays simple"
    )

with st.expander("Credit risk agent"):
    st.markdown(
        "Scores every retrieved loan for PD and LGD, then computes expected loss as "
        "`EL = PD x LGD x EAD` and aggregates it **exposure-weighted**. A naive mean across "
        # Escaped: Streamlit markdown reads an unescaped '$' as opening LaTeX math
        # and the next one as closing it, which ate the words between these two.
        "loans of different sizes would let a \\$1,000 loan move the portfolio number as much "
        "as a \\$35,000 one.\n\n"
        "A loan is only scored if it has a complete feature row. Where a bureau attribute the "
        "models need is missing -- most often a non-positive or absent debt-to-income ratio, "
        "which affects roughly 0.2% of the book -- the loan is excluded rather than scored on "
        "an imputed value, and the report states how many were excluded and why. Exposure, "
        "expected loss and regulatory capital are therefore totals over the scored loans, "
        "which is why that count can sit slightly below the number of rows retrieved."
    )

with st.expander("Interest rate & concentration agent"):
    st.markdown(
        "Buckets the loan book by months remaining on its fixed-rate term, sets a modelled "
        "deposit book against it, and reports the repricing gap per bucket -- then converts "
        "that gap into money: interest earned at each loan's own contractual rate, interest "
        "paid to depositors, net interest income and margin, and the change in the next twelve "
        "months of net interest income under parallel rate shocks of plus or minus 100 and 200 "
        "basis points. Also computes concentration (HHI) across both loan purpose and borrower "
        "state. Runs concurrently with the credit risk agent -- both depend only on the data "
        "agent's output, not on each other."
    )
    st.markdown(
        "<div class='method-note'>The asset side is entirely observed. The deposit side is "
        "entirely assumed, and section 7 states each assumption and the figures that inherit "
        "it.</div>",
        unsafe_allow_html=True,
    )

with st.expander("Compliance agent"):
    st.markdown(
        "Checks the computed metrics against internal risk-appetite ceilings and Basel III "
        "input floors, and computes actual regulatory capital (correlation, capital "
        "requirement, RWA) from the portfolio's own PD/LGD/EAD -- a calculation, not just a "
        "citation. Basel-sourced thresholds and internal thresholds are labelled distinctly "
        "so an internal number is never presented as a regulatory requirement."
    )

st.subheader("6. Compliance methodology -- and why there is no RAG here")
st.markdown(
    "<p class='method-lead'>Limit checking is fully deterministic. No model judgement is "
    "involved at this step, and no regulatory text is retrieved at query time.</p>",
    unsafe_allow_html=True,
)

with st.expander("Why the citations are hardcoded rather than retrieved", expanded=True):
    st.markdown(
        "A vector store over the Basel framework would be the fashionable choice and it "
        "would be the wrong one. There are exactly five limits and two of them are the "
        "*reason* a specific threshold value exists in the database at all -- the 0.05% PD "
        "floor and the 30% LGD floor were read from the primary BIS text and written into "
        "`Risk_Limits.description` together with the paragraph they came from. Retrieving "
        "that paragraph at query time could only introduce a way to fetch the wrong one.\n\n"
        "The three internal limits are not derived from Basel text at all. Basel's credit risk "
        "framework does not set portfolio-level sector concentration limits -- concentration "
        "sits under each bank's own risk-appetite framework, and separately, for single "
        "counterparties, under the Large Exposures framework, which does not apply to "
        "sector-level retail concentration. Nor does Basel impose a loan-to-deposit cap; it "
        "addresses funding stability through the Net Stable Funding Ratio instead. So those "
        "limits get an honest note saying they are internal policy, rather than a manufactured "
        "citation."
    )

with st.expander("Basel III IRB calculation as implemented"):
    st.code(
        "R   = 0.03 x (1 - e^(-35 x PD)) / (1 - e^-35)\n"
        "      + 0.16 x [1 - (1 - e^(-35 x PD)) / (1 - e^-35)]\n"
        "K   = LGD x N[(G(PD) + sqrt(R / (1 - R)) x G(0.999)) / sqrt(1 - R)] - PD x LGD\n"
        "RWA = K x 12.5 x EAD",
        language="text",
    )
    st.markdown(
        "**CRE31.16**, the IRB risk-weight function for the *Other Retail* exposure class -- "
        "the correct class for unsecured personal instalment loans, which are neither "
        "mortgages nor qualifying revolving exposures. Retail exposures carry no maturity "
        "adjustment: maturity is subsumed in the correlation assumption, per the BIS QIS3 "
        "FAQ. The **CRE32.58** input floors (PD 0.05%, LGD 30%) are applied before the "
        "formula is evaluated, and where K evaluates negative it is clipped to zero -- "
        "meaning the exposure attracts no regulatory capital rather than negative capital."
    )
    st.markdown(
        "<div class='method-note'>Every formula and floor value was checked against the "
        "primary BIS source text before implementation, not taken from memory or a secondary "
        "paraphrase.</div>",
        unsafe_allow_html=True,
    )

st.subheader("7. Known limitations")
st.markdown(
    "<p class='method-lead'>Stated plainly, because a risk system that hides its own "
    "assumptions is not auditable.</p>",
    unsafe_allow_html=True,
)

with st.expander("Assumptions in the interest rate risk view", expanded=True):
    st.markdown(
        "The asset side is real: balances, contractual interest rates, terms and issue dates "
        "all come from the retrieved rows. **Everything about the deposit side is assumed**, "
        "because this portfolio holds loans and no liability data exists to read."
    )
    st.markdown(
        "- **Deposit book size.** Modelled at **1.05x** the loan book, so the loan-to-deposit "
        "ratio lands near 0.95 -- deposits fund the loans plus the liquid assets a bank carries "
        "alongside them, which is why the multiple is above 1.0 rather than exactly 1.0.\n"
        "- **Deposit maturity profile.** Spread across the four buckets at "
        "**55 / 25 / 15 / 5 percent**, the short-weighted shape of a retail deposit book. Every "
        "bucket-level gap figure inherits this.\n"
        "- **Deposit rate.** Depositors are paid **5% of the rate earned on the loans**. At this "
        "book's roughly 13-14% weighted-average contractual rate that implies a deposit rate "
        "near 0.7%, which is what US retail savings accounts actually paid across the 2013-2018 "
        "window this data covers -- but it is a chosen pass-through, not an observed rate.\n"
        "- **Asset repricing is approximated** as the months remaining on each fixed-rate term. "
        "A fixed-rate instalment loan does not truly reprice; the bucket therefore represents "
        "when the cash flow rolls off, which is the standard simplification for a gap table but "
        "is not a full behavioural repricing model.\n"
        "- **The as-of date is the portfolio's, not today's.** Buckets are measured from the "
        "latest issue date in the retrieved rows. Measured against the wall clock this "
        "2013-2018 book has entirely matured, every loan would show zero months remaining, and "
        "the gap table would collapse into a single bar -- an artefact of when the report was "
        "run rather than a property of the portfolio.\n"
        "- **Rate shocks are parallel shifts only**, weighted by how much of the next twelve "
        "months each repriced balance is exposed for, on the standard midpoint convention. No "
        "prepayment behaviour, no non-parallel curve moves, no basis risk, and no "
        "economic-value-of-equity measure.\n"
        "- **The loan-to-deposit compliance check inherits all of the above.** It is a real "
        "check against a real internal limit, computed from a modelled denominator, and the "
        "compliance note attached to it says so."
    )

with st.expander("Limits and regulatory scope"):
    st.markdown(
        "- The internal thresholds are **illustrative** risk-appetite settings chosen for "
        "this project, not a real institution's calibrated limits.\n"
        "- Basel input floors are applied for unsecured **Other Retail** only. Secured, "
        "mortgage and qualifying-revolving exposures use different floors and a different "
        "correlation, and would need their own treatment.\n"
        "- No maturity adjustment is applied, correctly, for retail -- but this also means "
        "the implementation does not generalise to corporate exposures as written.\n"
        "- Only the credit-risk pillar of capital is computed. Operational and market risk "
        "capital, buffers above the 8% minimum, and the leverage ratio are out of scope."
    )

with st.expander("Model and data scope"):
    st.markdown(
        "- The PD model is **behavioural**, not application scoring. It answers \"how risky "
        "is this loan we already hold\", not \"should we approve this applicant\".\n"
        "- Data covers **US unsecured consumer instalment loans, 2013-2018**. Estimates are "
        "conditioned on that period's macro environment; nothing here is a forward-looking "
        "or point-in-time-adjusted forecast.\n"
        "- LGD R^2 of 0.235 means individual-loan severity predictions are weak even though "
        "portfolio-level aggregates are usable. Do not read a single loan's LGD as precise.\n"
        "- Retrieval retries are bounded at three, so a genuinely ambiguous question returns "
        "an honest failure rather than an eventually-plausible answer."
    )

with st.expander("What the interface deliberately does not show"):
    st.markdown(
        "The interface renders **only aggregated metrics** and never displays individual loan "
        "or borrower records -- not in a table, not in a preview, not in an expander. The "
        "reports and the PDF memo contain portfolio-level and segment-level figures only. "
        "This is a design constraint, not an omission: the underlying rows carry income, "
        "employment title, state, FICO band and delinquency history, and none of that needs "
        "to be on screen for a portfolio risk decision."
    )

st.subheader("8. Tech stack")
st.table(
    {
        "Technology": [
            "Python", "LangGraph", "Pydantic", "Ollama", "XGBoost / scikit-learn",
            "SQLite", "Streamlit", "Plotly", "pandas / NumPy", "reportlab / matplotlib",
        ],
        "Purpose": [
            "Core application and agent development",
            "Agent orchestration, parallel fan-out/fan-in, retry loops",
            "Structured state, tool results, and data validation throughout",
            "Local LLM inference (Gemma)",
            "PD classification and LGD regression models",
            "Live loan portfolio database (878K+ loans)",
            "Interactive risk report interface",
            "Interactive risk visualisations",
            "Feature engineering and financial calculations",
            "Downloadable PDF risk memo and its charts",
        ],
    }
)
st.caption(
    "The LLM runs locally, on this machine. Schema-guarding and SQL generation see only the "
    "schema description and the question; result evaluation additionally sees a small sample "
    "of the retrieved rows in order to judge whether they answer the question. No data leaves "
    "the machine, and the model performs none of the risk arithmetic and sets no threshold or "
    "citation."
)
