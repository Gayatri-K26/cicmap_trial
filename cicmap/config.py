"""Settings, read from the environment (.env in the project root)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.environ.get("CICMAP_DB", ROOT / "data" / "cicmap.db"))

SHEET_ID = os.environ.get("CICMAP_SHEET_ID", "1ZuszRylzO41_o89qH3KFZ-h92yjR6qC30Ycp6w8IN3o")
SHEET_GID = os.environ.get("CICMAP_SHEET_GID", "0")
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
SHEET_TTL_HOURS = float(os.environ.get("CICMAP_SHEET_TTL_HOURS", "6"))

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

USER_AGENT = os.environ.get(
    "CICMAP_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36",
)
REQUEST_DELAY_SECONDS = float(os.environ.get("CICMAP_REQUEST_DELAY", "0.5"))
