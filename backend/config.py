import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def project_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "qbp-meeting-dev-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'db' / 'qbp.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = Path(os.environ.get("UPLOAD_FOLDER", PROJECT_ROOT / "data" / "uploads"))
    POWERPOINT_PREVIEW_FOLDER = Path(
        os.environ.get("POWERPOINT_PREVIEW_FOLDER", PROJECT_ROOT / "data" / "previews")
    )
    ZHISHU_BASE_URL = os.environ.get("ZHISHU_BASE_URL", "http://127.0.0.1:4173").rstrip("/")
    ZHISHU_API_KEY = os.environ.get("ZHISHU_API_KEY", "")
    ZHISHU_KNOWLEDGE_BASE_URL = os.environ.get("ZHISHU_KNOWLEDGE_BASE_URL", ZHISHU_BASE_URL).rstrip("/")
    ZHISHU_KNOWLEDGE_API_KEY = os.environ.get("ZHISHU_KNOWLEDGE_API_KEY", ZHISHU_API_KEY)
    ZHISHU_KNOWLEDGE_AUTH_HEADER = os.environ.get("ZHISHU_KNOWLEDGE_AUTH_HEADER", "Authorization")
    ZHISHU_CLIENT = None
    ZHISHU_KNOWLEDGE_ENABLED = os.environ.get("ZHISHU_KNOWLEDGE_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    ZHISHU_KNOWLEDGE_CLIENT = None
    MATERIAL_RAG_TOP_K = int(os.environ.get("MATERIAL_RAG_TOP_K", "12"))
    MATERIAL_RAG_CHUNK_CHARS = int(os.environ.get("MATERIAL_RAG_CHUNK_CHARS", "1400"))
    MATERIAL_RAG_CHUNK_OVERLAP = int(os.environ.get("MATERIAL_RAG_CHUNK_OVERLAP", "150"))
    MATERIAL_RAG_INDEX_TIMEOUT_SECONDS = int(os.environ.get("MATERIAL_RAG_INDEX_TIMEOUT_SECONDS", "60"))
    MATERIAL_RAG_VECTOR_ENABLED = os.environ.get("MATERIAL_RAG_VECTOR_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    MATERIAL_RAG_VECTOR_DB_PATH = project_path(
        os.environ.get("MATERIAL_RAG_VECTOR_DB_PATH", PROJECT_ROOT / "data" / "vector" / "lancedb")
    )
    MATERIAL_RAG_LOCAL_EMBEDDING_ENABLED = os.environ.get("MATERIAL_RAG_LOCAL_EMBEDDING_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    MATERIAL_RAG_LOCAL_EMBEDDING_MODEL_PATH = project_path(
        os.environ.get(
            "MATERIAL_RAG_LOCAL_EMBEDDING_MODEL_PATH",
            PROJECT_ROOT / "embedding_model" / "sentence-transformers" / "all-MiniLM-L6-v2",
        )
    )
    MATERIAL_RAG_LOCAL_EMBEDDING_BATCH_SIZE = int(os.environ.get("MATERIAL_RAG_LOCAL_EMBEDDING_BATCH_SIZE", "16"))
    MATERIAL_RAG_EMBEDDING_MODEL = os.environ.get("MATERIAL_RAG_EMBEDDING_MODEL", "")
    MATERIAL_RAG_EMBEDDING_BATCH_SIZE = int(os.environ.get("MATERIAL_RAG_EMBEDDING_BATCH_SIZE", "16"))
    MATERIAL_RAG_VECTOR_WEIGHT = float(os.environ.get("MATERIAL_RAG_VECTOR_WEIGHT", "0.75"))
    MATERIAL_RAG_KEYWORD_WEIGHT = float(os.environ.get("MATERIAL_RAG_KEYWORD_WEIGHT", "0.25"))
    MATERIAL_RAG_EMBEDDING_CLIENT = None
    MATERIAL_RAG_VECTOR_STORE = None
    COPILOT_DEFAULT_MODEL = os.environ.get("COPILOT_DEFAULT_MODEL", "")
    COPILOT_FUNCTION_CALLING = os.environ.get("COPILOT_FUNCTION_CALLING", "default")
    COPILOT_TOOL_IDS = [
        tool_id.strip()
        for tool_id in os.environ.get("COPILOT_TOOL_IDS", "").split(",")
        if tool_id.strip()
    ]
    QBP_PUBLIC_BASE_URL = os.environ.get("QBP_PUBLIC_BASE_URL", "http://127.0.0.1:5008").rstrip("/")
    KKFILEVIEW_BASE_URL = os.environ.get("KKFILEVIEW_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
    KKFILEVIEW_ENABLED = os.environ.get("KKFILEVIEW_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    QBP_FILEVIEW_BASE_URL = os.environ.get("QBP_FILEVIEW_BASE_URL", QBP_PUBLIC_BASE_URL).rstrip("/")
    FILEVIEW_TOKEN_TTL_SECONDS = int(os.environ.get("FILEVIEW_TOKEN_TTL_SECONDS", "300"))
    QBP_TOOL_SERVER_TOKEN = os.environ.get("QBP_TOOL_SERVER_TOKEN", "")
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Shanghai")
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024
    ALLOWED_EXTENSIONS = {
        "ppt",
        "pptx",
        "doc",
        "docx",
        "pdf",
        "xls",
        "xlsx",
        "png",
        "jpg",
        "jpeg",
    }


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    MATERIAL_RAG_VECTOR_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
