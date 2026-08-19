#!/usr/bin/env python3
"""
bluesky_weather_bot entry point.

Run from the project root:
    .venv/bin/python3 main.py          # recommended — explicit venv
    source .venv/bin/activate && python3 main.py

Or install and run as a module:
    python -m bluesky_weather_bot
"""

import logging
import sys
from pathlib import Path

# Ensure project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent))

# Guard against being run with the wrong Python / venv.
# pgeocode is a required dependency; a missing import means the wrong
# interpreter was used and zip-code lookups will silently fail.
try:
    import pgeocode  # noqa: F401
except ImportError:
    print(
        "ERROR: pgeocode not found — wrong Python interpreter?\n"
        "Run with:  .venv/bin/python3 main.py",
        file=sys.stderr,
    )
    sys.exit(1)

from bluesky_weather_bot.config.settings import Settings, ConfigError
from bot import build_bot


def setup_logging(settings: Settings) -> None:
    """Configures root logging to write to both stdout (for `journalctl
    -f`/foreground runs) and settings.log_path, at settings.log_level."""
    log_path = settings.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path),
    ]
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> None:
    """Process entry point: loads config, sets up logging, builds the bot
    via build_bot(), and blocks running it until Ctrl+C/SIGTERM."""
    try:
        settings = Settings.load(".local.env")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .local.env.example to .local.env and fill in your credentials.")
        sys.exit(1)

    setup_logging(settings)
    logger = logging.getLogger(__name__)
    logger.info("Starting bluesky_weather_bot...")

    bot = build_bot(settings)
    bot.start(block=True)   # blocks until Ctrl+C or SIGTERM


if __name__ == "__main__":
    main()
