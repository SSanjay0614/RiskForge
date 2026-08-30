"""RiskForge -- application entrypoint.

Kept at Frontend/app.py so the documented `streamlit run Frontend/app.py` command
still works, but it no longer holds the interface itself: the pages live in
Frontend/views/ and are declared below.

Streamlit derives a page's sidebar label from its filename when pages are
auto-discovered, which is how the main page ended up labelled "app". Declaring
the pages explicitly through st.navigation lets each one carry a real title, and
puts set_page_config and the stylesheet in exactly one place for both.
"""

from pathlib import Path
import sys

import streamlit as st

FRONTEND_DIR = Path(__file__).resolve().parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

# theme also bootstraps sys.path so the backend packages import cleanly.
from theme import inject_css  # noqa: E402

# Must be the first Streamlit call in the process, which is the reason the pages
# are declared here rather than each configuring itself.
st.set_page_config(
    page_title="RiskForge",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_css()

# Paths are relative to this script, which is what st.Page expects.
PAGES = [
    st.Page("views/risk_analysis.py", title="Risk Analysis", default=True),
    st.Page("views/methodology.py", title="Methodology & Transparency"),
]

st.navigation(PAGES).run()
