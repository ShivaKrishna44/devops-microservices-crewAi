import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("devops-agent")

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── LangSmith tracing ─────────────────────────────────────────────────────────
os.environ["LANGSMITH_TRACING"] = "true"

# ── Validated settings ────────────────────────────────────────────────────────
class Settings:
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_DEFAULT_REGION: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
    TARGET_REPO: str = os.getenv("TARGET_REPO", "ShivaKrishna44/devops-microservices-nojenkins")
    # CrewAI uses OPENAI_API_KEY env var for LLM. We map Groq through it.
    CREWAI_LLM: str = os.getenv("CREWAI_LLM", "groq/llama-3.3-70b-versatile")

settings = Settings()

# ── Set env vars that CrewAI/LiteLLM expects ─────────────────────────────────
os.environ["GROQ_API_KEY"] = settings.GROQ_API_KEY
