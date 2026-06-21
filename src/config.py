"""
config.py — Central configuration, loaded from the environment.

No secret is ever hardcoded in source. Everything sensitive (API keys, Odoo
credentials) is read from a local .env file that is git-ignored. This is the
single place that reads os.environ, so the rest of the codebase never touches
raw credentials.

Fail fast: if a required variable is missing we raise on import rather than
discovering it halfway through a run against a production ERP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    extraction_model: str

    odoo_url: str
    odoo_db: str
    odoo_username: str
    odoo_api_key: str


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        # Default chosen deliberately: a small, cheap, fast model is plenty for
        # structured field extraction. Overridable via env for experiments.
        extraction_model=os.getenv("EXTRACTION_MODEL", "claude-sonnet-4-6"),
        odoo_url=os.getenv("ODOO_URL", "http://localhost:8069"),
        odoo_db=_require("ODOO_DB"),
        odoo_username=_require("ODOO_USERNAME"),
        odoo_api_key=_require("ODOO_API_KEY"),
    )
