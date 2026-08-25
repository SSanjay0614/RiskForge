# 🛡️ RiskForge

### Multi-Agent Loan Portfolio Risk Intelligence System

![RiskForge Architecture](docs/RiskForge_Architecture.png)

RiskForge is an **agentic AI platform** that analyzes a bank's loan portfolio across credit, interest rate, concentration, and regulatory compliance risk through four specialized agents — turning a natural-language question into an **auditable, source-attributed risk report**.

Built with **LangGraph, Pydantic, Streamlit, and a locally-hosted LLM (Ollama)**, RiskForge combines trained ML risk models, deterministic financial calculations, and verified Basel III regulatory logic into a single query-driven workflow — with no data ever leaving the local machine.

---

## 🚀 Why RiskForge?

Portfolio risk analysis usually means stitching together separate spreadsheets, models, and compliance checklists by hand — a credit risk view here, a concentration report there, a regulatory capital calculation somewhere else, with no single place that ties them back to the actual underlying loan data or cites where a number came from.

RiskForge unifies this into one pipeline: a natural-language question is translated into a real SQL query against a live loan portfolio database, validated and self-corrected if the first attempt misses, then — only when genuinely needed — routed through parallel risk agents that compute expected loss, interest rate exposure, concentration, and regulatory capital, with every compliance flag traced back to either an internal policy threshold or a specific, verified Basel III paragraph.

The system distinguishes between queries that need full risk analysis and queries that don't (a simple count or lookup returns instantly, without being buried under an irrelevant risk report), and every number in the final output is attributable to either the portfolio data itself, a trained model, a verified financial formula, or a cited regulatory source — never an unexplained figure.

---

## ✨ Features

### 🗄️ Data Agent — *Agentic Retrieval*
- Translates natural language into SQL against a live loan portfolio (878K+ real loan records)
- Schema-relevance guard short-circuits questions the data genuinely can't answer
- Self-corrects: evaluates its own retrieved data and retries with feedback on a bounded loop, rather than failing silently or looping forever
- Enforces genuine read-only database access (not just a prompt instruction) and a row-count safety net against pathological queries
- Distinguishes simple lookup/count queries from questions that require full risk analysis, so a simple answer stays simple

### 💳 Credit Risk Agent
- Feature-engineers retrieved loan data through the same pipeline used to train the underlying models (no train/serve skew)
- Scores loans with a **behavioral PD model** (XGBoost, calibrated, leakage-audited) — trained to assess *existing* loans, not new-applicant underwriting
- Scores **Loss Given Default** via a dedicated regression model trained on real historical recovery data, using exposure-at-default (not original loan size) as the basis, per standard credit-risk methodology
- Computes portfolio/segment-level **Expected Loss** (EL = PD × LGD × EAD), exposure-weighted — not a naive average across loans of different sizes

### 📈 Interest Rate & Concentration Agent
- Computes **repricing gap** (interest rate risk) across maturity buckets from real loan terms and issue dates
- Computes portfolio **concentration risk (HHI)** across both sector (loan purpose) and region (borrower state)
- Runs in genuine parallel with the Credit Risk Agent — both depend only on the Data Agent's output, not on each other

### ⚖️ Compliance Agent
- Checks computed portfolio metrics against both **internal risk-appetite limits** and **Basel III regulatory floors** (PD/LGD input floors, verified letter-for-letter against the primary BIS source)
- Computes actual **Basel III regulatory capital** (correlation, capital requirement, risk-weighted assets) using the verified IRB "Other Retail" formula — not just a citation, a real calculation using the portfolio's own PD/LGD/EAD
- Clearly distinguishes Basel-sourced thresholds from internal policy thresholds, rather than implying an internal risk-appetite number is a regulatory requirement

### 🧭 Agentic Workflow
- LangGraph-orchestrated multi-agent pipeline with a genuine fan-out/fan-in structure (parallel agents, not a linear script)
- Bounded, self-correcting retry loop at the data-retrieval layer — the system can recognize and fix its own mistakes before bad data reaches any risk calculation
- Partial-state graph updates throughout, correctly supporting real parallel execution (not just sequential steps dressed up as agents)

### 🔍 Model Rigor
- Behavioral PD model independently audited for data leakage (a suspiciously high initial AUC was traced, diagnosed, and fixed — see project documentation)
- LGD target defined relative to exposure-at-default, not original loan size, per correct credit-risk methodology
- Every Basel III formula and floor value verified directly against primary source text before implementation, not taken from memory or secondary paraphrase

---

## 🛠️ Tech Stack

| Technology | Purpose |
|---|---|
| Python | Core application and agent development |
| LangGraph | Agent orchestration, parallel fan-out/fan-in, retry loops |
| Pydantic | Structured state, tool results, and data validation throughout |
| Ollama | Local LLM inference (Gemma) |
| XGBoost / scikit-learn | PD classification and LGD regression models |
| SQLite | Live loan portfolio database (878K+ loans) |
| Streamlit | Interactive risk report interface |
| Plotly | Interactive risk visualizations |
| pandas / NumPy | Feature engineering and financial calculations |

## 🚀 Getting Started

### Clone the Repository

```bash
git clone https://github.com/SSanjay0614/RiskForge.git
cd RiskForge
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Prepare the Data and Models

Create the `Data` folder, place the dataset inside it, and run the notebooks in the `Notebooks` folder in order to generate the models in the `Models` folder.

### Pull the Local LLM

```bash
ollama pull gemma4
```

### Set Up the Database

```bash
python -m Database.init_db
python -m Database.seed_db
python -m Database.add_basel_limits
```

### Launch RiskForge

```bash
streamlit run Frontend/app.py
```

The application opens with a single conversational interface. Ask a question about the loan portfolio — a simple lookup ("how many loans have sub_grade B3?") returns instantly; a risk question ("what's our expected loss and concentration risk for California loans?") runs the full agentic pipeline and returns an auditable report with every figure attributable to its source.

---


## 👨‍💻 Author

Sanjay S

B.Tech Computer Science and Engineering

VIT Chennai