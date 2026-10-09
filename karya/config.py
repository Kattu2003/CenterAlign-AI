"""Runtime settings, read once from the environment (and `.env`).

A plain frozen dataclass instead of pydantic-settings: one less dependency, and
every setting is visible in one place. Relative paths are resolved against the
repository root so the CLI behaves the same from any working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def env_str(name: str, default: str = "") -> str:
    """Read an env var. A value that starts with '#' counts as empty: python-dotenv reads
    `LLM_MODEL=   # comment` (an empty value followed by a comment) as the comment text."""
    value = os.environ.get(name, default).strip()
    return default if value.startswith("#") else value


_str = env_str


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def resolve_path(value: str | Path) -> Path:
    """Resolve a configured path; relative paths are relative to the repo root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str
    llm_model: str
    llm_model_fast: str
    azure_endpoint: str
    azure_api_key: str
    azure_api_version: str
    openai_api_key: str
    anthropic_api_key: str
    # runtime
    company_dir: Path
    db_path: Path
    runs_dir: Path
    headless: bool
    max_steps: int
    max_steps_batch: int
    task_token_budget: int
    task_wallclock_min: int
    snapshot_chars: int
    history_full_steps: int
    # optional extras (not in .env.example; sensible defaults)
    injection_llm_check: bool
    chromium_path: str
    sandbox_admin_token: str
    openai_base_url: str      # any OpenAI-compatible API, e.g. Gemini's
    llm_max_rpm: int          # pace model calls under a free tier's cap; 0 = no pacing
    browser_window: str       # 'x,y,width,height' for the visible browser (demo recordings)

    @property
    def fast_model(self) -> str:
        """The model used for extraction, fact rewriting and injection checks."""
        return self.llm_model_fast or self.llm_model


def load_settings() -> Settings:
    # Process environment wins over .env so tests and CI can override anything.
    load_dotenv(ROOT / ".env", override=False)
    return Settings(
        llm_provider=_str("LLM_PROVIDER", "azure_openai").lower(),
        llm_model=_str("LLM_MODEL"),
        llm_model_fast=_str("LLM_MODEL_FAST"),
        azure_endpoint=_str("AZURE_OPENAI_ENDPOINT"),
        azure_api_key=_str("AZURE_OPENAI_API_KEY"),
        azure_api_version=_str("AZURE_OPENAI_API_VERSION"),
        openai_api_key=_str("OPENAI_API_KEY"),
        anthropic_api_key=_str("ANTHROPIC_API_KEY"),
        company_dir=resolve_path(_str("KARYA_COMPANY_DIR", "company")),
        db_path=resolve_path(_str("KARYA_DB_PATH", "data/karya.db")),
        runs_dir=resolve_path(_str("KARYA_RUNS_DIR", "runs")),
        headless=_bool("KARYA_HEADLESS", True),
        max_steps=_int("KARYA_MAX_STEPS", 60),
        max_steps_batch=_int("KARYA_MAX_STEPS_BATCH", 150),
        task_token_budget=_int("KARYA_TASK_TOKEN_BUDGET", 800_000),
        task_wallclock_min=_int("KARYA_TASK_WALLCLOCK_MIN", 25),
        snapshot_chars=_int("KARYA_SNAPSHOT_CHARS", 8000),
        history_full_steps=_int("KARYA_HISTORY_FULL_STEPS", 6),
        injection_llm_check=_bool("KARYA_INJECTION_LLM_CHECK", True),
        chromium_path=_str("KARYA_CHROMIUM_PATH"),
        sandbox_admin_token=_str("SANDBOX_ADMIN_TOKEN", "change-me-admin"),
        openai_base_url=_str("OPENAI_BASE_URL"),
        llm_max_rpm=_int("KARYA_LLM_MAX_RPM", 0),
        browser_window=_str("KARYA_BROWSER_WINDOW"),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Forget cached settings (tests change environment variables between cases)."""
    global _settings
    _settings = None
