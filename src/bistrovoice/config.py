"""Paths and settings. Everything is read from environment variables (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
WEB_DIR = Path(__file__).resolve().parent / "web"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    stt_model: str
    stt_backend: str
    tts_voice: str
    tts_backend: str

    @classmethod
    def from_env(cls) -> Settings:
        db_path = Path(os.getenv("DB_PATH", "var/bistrovoice.db"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        return cls(
            db_path=db_path,
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            llm_api_key=os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "openai/gpt-oss-120b"),
            stt_model=os.getenv("STT_MODEL", "base.en"),
            stt_backend=os.getenv("STT_BACKEND", "local"),
            tts_voice=os.getenv("TTS_VOICE", "en_GB-jenny_dioco-medium"),
            # piper = synthesised on the server (default, used by the evaluation).
            # browser = the visitor's browser speaks the reply with an English
            # system voice; for hosts too small to run Piper quickly.
            tts_backend=os.getenv("TTS_BACKEND", "piper"),
        )
