"""
Configuration loader.

Reads settings from .local.env (or any dotenv-compatible file).
All required values are validated on load; missing keys raise ConfigError.

Usage:
    from bluesky_weather_bot.config.settings import Settings
    cfg = Settings.load()
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    # Bluesky credentials
    bluesky_handle: str
    bluesky_app_password: str

    # Storage
    db_path: Path

    # File alert channel
    inbox_path: Path
    inbox_archive_path: Path
    inbox_error_path: Path
    inbox_poll_interval_sec: float

    # Logging
    log_path: Path
    log_level: str

    # Weather
    weather_cache_ttl_minutes: int
    skip_historical: bool

    # Output mode
    post_mode: str   # "text" | "image"

    # Public-mention alert backend(s) — comma-delimited, e.g. "firehose,jetstream"
    mention_backends: frozenset[str]

    # Server identification (shown in latency footer)
    server_type: str  # "laptop" | "Pi"

    @classmethod
    def load(cls, env_file: str | Path = ".local.env") -> "Settings":
        _load_dotenv(env_file)

        def req(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise ConfigError(f"Required config key {key!r} missing from {env_file}")
            return val

        def opt(key: str, default: str = "") -> str:
            return os.getenv(key, default).strip()

        def opt_bool(key: str, default: bool = False) -> bool:
            return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes")

        def opt_float(key: str, default: float) -> float:
            try:
                return float(os.getenv(key, str(default)))
            except ValueError:
                return default

        def opt_int(key: str, default: int) -> int:
            try:
                return int(os.getenv(key, str(default)))
            except ValueError:
                return default

        return cls(
            bluesky_handle=req("BSKY_HANDLE"),
            bluesky_app_password=req("BSKY_APP_PASSWORD"),
            db_path=Path(opt("DB_PATH", "data/zipwx.db")),
            inbox_path=Path(opt("INBOX_PATH", "inbox")),
            inbox_archive_path=Path(opt("INBOX_ARCHIVE_PATH", "inbox/archive")),
            inbox_error_path=Path(opt("INBOX_ERROR_PATH", "inbox/errors")),
            inbox_poll_interval_sec=opt_float("INBOX_POLL_INTERVAL_SEC", 5.0),
            log_path=Path(opt("LOG_PATH", "logs/zipwx.log")),
            log_level=opt("LOG_LEVEL", "INFO").upper(),
            weather_cache_ttl_minutes=opt_int("WEATHER_CACHE_TTL_MINUTES", 30),
            skip_historical=opt_bool("SKIP_HISTORICAL", False),
            post_mode=opt("POST_MODE", "text").lower(),
            server_type=opt("SERVER_TYPE", "laptop"),
            mention_backends=_req_choice_set(
                opt("MENTION_BACKEND", "firehose").lower(),
                "MENTION_BACKEND", {"firehose", "jetstream"},
            ),
        )

    def ensure_directories(self) -> None:
        """Creates all configured runtime directories if they don't exist."""
        for p in [
            self.db_path.parent,
            self.inbox_path,
            self.inbox_archive_path,
            self.inbox_error_path,
            self.log_path.parent,
        ]:
            p.mkdir(parents=True, exist_ok=True)


def _req_choice_set(value: str, key: str, allowed: set[str]) -> frozenset[str]:
    """Parses a comma-delimited value (e.g. "firehose,jetstream") into a
    validated set. At least one entry is required."""
    items = frozenset(v.strip() for v in value.split(",") if v.strip())
    if not items:
        raise ConfigError(f"{key} must specify at least one of {sorted(allowed)}")
    invalid = items - allowed
    if invalid:
        raise ConfigError(f"{key} contains invalid value(s) {sorted(invalid)} — must be one of {sorted(allowed)}")
    return items


def _load_dotenv(env_file: str | Path) -> None:
    """Load dotenv file into os.environ. Falls back to manual parse if python-dotenv absent."""
    path = Path(env_file)
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=path, override=False)
    except ImportError:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
