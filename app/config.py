import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))

_default_data_dir = Path("/tmp/kharch_karod_data") if IS_VERCEL else (BASE_DIR / "data")
DATA_DIR = Path(os.getenv("DATA_DIR", str(_default_data_dir)))
UPLOAD_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_raw_db_url = os.getenv("DATABASE_URL", "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)

DATABASE_URL = _raw_db_url or f"sqlite:///{DATA_DIR / 'kharch_karod.db'}"
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "kk-dev-secret-change-in-production-9f4a2e8c1d")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 72
MAX_UPLOAD_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB local; 4.5 MB on Vercel Serverless
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")
