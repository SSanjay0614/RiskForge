from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()
# Resolves to CreditRisk/ -- this file must live directly at the project root
# for a single .parent to be correct. If you ever move it into a subfolder
# (e.g. config/settings.py, like ResearchForge does), change this to
# .parent.parent instead.
PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "Data"
MODELS_DIR = PROJECT_ROOT / "Models"
TOOLS_DIR = PROJECT_ROOT / "tools"
PLAYGROUND_DIR = PROJECT_ROOT / "Playground"
DMODELS_DIR = PROJECT_ROOT / "dmodels"

# --- PD model artifacts ---
PD_MODEL_PATH = MODELS_DIR / "pd_model_calibrated.joblib"
PD_FEATURE_NAMES_PATH = MODELS_DIR / "pd_model_feature_names.joblib"
PD_TIER_CUTOFFS_PATH = MODELS_DIR / "pd_risk_tier_cutoffs.joblib"

# --- LGD model artifacts ---
LGD_MODEL_PATH = MODELS_DIR / "lgd_model.joblib"
LGD_FEATURE_NAMES_PATH = MODELS_DIR / "lgd_model_feature_names.joblib"

# --- Processed data (parquet) ---
BEHAVIORAL_PD_PARQUET = DATA_DIR / "model_df_behavioral.parquet"
LGD_PROCESSED_PARQUET = DATA_DIR / "lgd_df_processed.parquet"

DATABASE_DIR = PROJECT_ROOT / "Database"
DB_PATH = DATABASE_DIR / "credit_risk.db"

ADDR_STATE_FREQ_MAP_PATH = MODELS_DIR / "addr_state_freq_map.joblib"
EMP_TITLE_FREQ_MAP_PATH = MODELS_DIR / "emp_title_freq_map.joblib"

RAW_CSV_PATH = DATA_DIR / "accepted_2007_to_2018Q4.csv"

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4")

TEMPERATURE = 0.1