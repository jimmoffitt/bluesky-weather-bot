# bluesky_weather_bot

A modular Bluesky bot that responds to weather requests from public posts,
direct messages, and file-based alerts.

## What this project does
Users post `#ZipWx Denver, CO` or `#ZipWx 80501` on Bluesky (or send a DM,
or drop a YAML file in the inbox) and receive a 3-post weather thread:
current conditions, 6-hour forecast, and historical comparison —
all in dual units (°F/°C, mph/km/h, in/mm).

## Architecture in one diagram
```
Alert channels (inputs)               Notify channels (outputs)
───────────────────────               ─────────────────────────
FileWatcherAlertChannel   ──┐
FirehoseAlertChannel      ──┼──▶  ZipWx (bot.py)  ──▶  BlueskyPostNotifyChannel
DMAlertChannel            ──┘         │            ──▶  BlueskyDMNotifyChannel
                                      ▼
                                 WeatherService
                               (Open-Meteo API, free)
                                      ▼
                                   Database
                                  (SQLite WAL)
```

## Package structure
```
bluesky_weather_bot/        ← importable package
  channels/
    alert/
      base.py               ← AlertChannel ABC + AlertRequest dataclass
      file_watcher.py       ← polls inbox/ for YAML files every 5s
      firehose.py           ← Bluesky public post firehose listener
      dm_poller.py          ← Bluesky DM poller (15s interval)
    notify/
      base.py               ← NotificationChannel ABC + payload/result
      bluesky_post.py       ← posts 3-post public reply thread
      bluesky_dm.py         ← sends DM reply
  weather/
    models.py               ← dataclasses: WeatherReport, CurrentConditions, etc.
    resolver.py             ← zip/city → lat/lon + timezone (pgeocode + Nominatim)
    client.py               ← Open-Meteo API client (no key required)
    formatter.py            ← WeatherReport → list of Bluesky post strings (≤300 chars each)
    service.py              ← WeatherService facade: raw location → list[WeatherReport]
  storage/
    db.py                   ← SQLite: requests, responses, weather_cache, seen_dm_ids, file_inbox_log
  config/
    settings.py             ← loads .local.env → Settings dataclass
bot.py                      ← ZipWx orchestrator + build_bot() factory
main.py                     ← entry point
```

## Key design decisions
- **Alert channels** are inputs; **notify channels** are outputs. They never talk to each other —
  everything routes through the orchestrator in `bot.py`.
- Adding a new alert channel: subclass `AlertChannel`, implement `start()`/`stop()`,
  register in `build_bot()`. No other changes needed.
- Adding a new notify channel: subclass `NotificationChannel`, implement `send()`,
  register in `build_bot()` and add a routing rule in `ZipWx._route()`.
- `WeatherService.lookup(raw_location)` is the single weather entry point.
  Returns `list[WeatherReport]` — usually one, two for ambiguous cities like "Portland".
- Bluesky post limit is 300 chars. Formatter produces a 3-post thread.
- All timestamps are ISO8601 UTC strings in the DB.

## Database schema (SQLite)
Five tables: `requests`, `responses`, `weather_cache`, `seen_dm_ids`, `file_inbox_log`.
One view: `latency_summary` — computes receive / processing / delivery / end-to-end latency.
Latency is tracked at four points in bot.py (see Latency section below).
Requests over 30s end-to-end are flagged `is_slow = 1`.
Retention: requests/responses pruned after 90 days. Cache TTL: 1 hour.

## Latency instrumentation
bot.py must record these timestamps in order:
1. `source_created_at`        — from AT Protocol record (passed in from channel)
2. `ingested_at`              — set by alert channel before calling `_dispatch()`
3. `processing_started_at`    — `db.update_request_processing_start()` before weather lookup
4. `processing_finished_at`   — `db.update_request_processing_finish()` after formatting
5. `delivery_started_at`      — captured in bot.py just before `channel.send()`
6. `delivery_finished_at`     — captured in bot.py just after `channel.send()` returns

## Configuration
All settings loaded from `.local.env` (gitignored). Copy `.local.env.example` to get started.
Required: `BSKY_HANDLE`, `BSKY_APP_PASSWORD`.
Key optional: `INBOX_PATH`, `DB_PATH`, `LOG_PATH`, `SKIP_HISTORICAL`, `WEATHER_CACHE_TTL_MINUTES`.

## Weather data
- Provider: Open-Meteo (free, no API key, US + global coverage)
- Current conditions: temp, feels-like, humidity, wind, gusts, cloud cover, pressure, visibility
- Forecast: next 6 hours (temp, precip probability, wind, cloud cover)
- Historical: same date last year + 10-year climatological average
- Location resolution: zip codes via `pgeocode` (offline), top ~150 US cities hardcoded,
  ambiguous cities (Portland, Springfield, etc.) return results for all candidates,
  fallback via Nominatim geocoding

## Inbox YAML format (file alert channel)
```yaml
full_message: 'Red Rocks Park 30-day rain: 0.71 inches #COWx'
message: 'Red Rocks Park 30-day rain: 0.71 inches'
created_at: '2025-02-19T17:30:07+07:00'
host: Test
tags: [COWx, Rain]
mentions:
```

## Reference projects (in ~/projects/)
- `bluesky_listener/`   — working firehose listener; use for firehose.py implementation
- `streaming_bluesky/`  — working post/reply publisher; use for bluesky_post.py implementation
- `alert_stream/`       — working file watcher; use for file_watcher.py implementation

## What's complete vs. stubbed
- `weather/models.py`     ✅ complete
- `weather/resolver.py`   ✅ complete
- `weather/client.py`     ✅ complete
- `weather/formatter.py`  ✅ complete
- `weather/service.py`    ✅ complete
- `storage/db.py`         ✅ complete
- `config/settings.py`    ✅ complete
- `bot.py`                ✅ complete (orchestrator logic written; needs testing)
- `main.py`               ✅ complete
- `channels/alert/base.py`         ✅ complete
- `channels/notify/base.py`        ✅ complete
- `channels/alert/file_watcher.py` 🔧 stub — adapt from `alert_stream/`
- `channels/alert/firehose.py`     🔧 stub — adapt from `bluesky_listener/`
- `channels/alert/dm_poller.py`    🔧 stub — adapt from `bluesky_listener/`
- `channels/notify/bluesky_post.py`🔧 stub — adapt from `streaming_bluesky/`
- `channels/notify/bluesky_dm.py`  🔧 stub — adapt from `streaming_bluesky/`

## Running / testing
```bash
source .venv/bin/activate

# Validate weather core (no Bluesky credentials needed):
SKIP_HISTORICAL=1 python -c "
from bluesky_weather_bot.storage.db import Database
from bluesky_weather_bot.weather.service import WeatherService
from bluesky_weather_bot.weather.formatter import WeatherFormatter
with Database(':memory:') as db:
    svc = WeatherService(db, skip_historical=True)
    for post in WeatherFormatter().format_thread(svc.lookup('Longmont, CO')[0]):
        print(post); print('---')
"

# Run the bot:
python main.py
```
