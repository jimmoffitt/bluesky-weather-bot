# bluesky_weather_bot

A modular Bluesky bot that responds to weather requests from public posts,
direct messages, and file-based alerts. Reply with a portrait image card
or a 3-post text thread — your choice.

## What it looks like

**Image mode** (`POST_MODE=image`) — a single portrait card posted as a reply:

<img src="docs/screenshot_image_card.png" width="300" alt="Weather image card for Denver, CO showing 54°F clear sky with humidity, wind, pressure and visibility stats">

**Text mode** (`POST_MODE=text`) — a 3-post thread (also used for DM replies):

```
📍 New York, NY | Fri Mar 13 1:30 AM EDT
☀️ Clear sky
🌡 32°F (0°C) | Feels 24°F (-4°C)
💧 Humidity: 72%
💨 Wind: 8mph (12km/h) W | Gusts 22mph (35km/h)
🌧 Precip: 0.00in (0.0mm)
👁 Visibility: 49.9mi (80.4km)
📊 Pressure: 1016hPa
---
⏱ Next 6 Hours — New York, NY
1AM: 32°F, ☁ 0%, 💧 0%, 💨 8mph
2AM: 32°F, ☁ 0%, 💧 0%, 💨 6mph
...
---
📅 Historical — New York, NY
Last year (Mar 12, 2025):
  Hi 52°F (11°C) / Lo 38°F (3°C) | Precip 0.00in
10-yr avg (Mar ±7d):
  Hi 56°F (13°C) / Lo 38°F (3°C) | Precip 0.20in
```

## Quick start

```bash
# 1. Clone and set up environment
cd ~/projects/bluesky_weather_bot
python3 -m venv .venv && source .venv/bin/activate
pip3 install -r requirements.txt

# 2. Configure credentials
cp .local.env.example .local.env
# Edit .local.env — set BSKY_HANDLE, BSKY_APP_PASSWORD, and POST_MODE

# 3. Run (single instance — do not start multiple)
python3 main.py
```

> **Note:** Always ensure only one instance is running. Multiple processes each
> respond to every incoming post, resulting in duplicate replies.

## Triggering the bot

Users trigger the bot by:

| Method | Example |
|--------|---------|
| Public post — zip code | `#ZipWx 80501` |
| Public post — city | `#ZipWx Denver, CO` |
| Public post — city only | `#ZipWx Portland` *(returns results for all matches)* |
| Mention | `@zipwx.bsky.social 94102` |
| Direct message | Send any of the above to the bot |
| File inbox | Drop a YAML file into `inbox/` |

The bot replies to the original post (public) or sends a DM response. Image
mode is used for public replies; DMs always use the text thread format since
Bluesky DMs do not support image attachments.

## Configuration

All settings are loaded from `.local.env` (gitignored). Copy
`.local.env.example` to get started.

| Variable | Default | Description |
|----------|---------|-------------|
| `BSKY_HANDLE` | *(required)* | Your bot's Bluesky handle |
| `BSKY_APP_PASSWORD` | *(required)* | App password from Bluesky settings |
| `POST_MODE` | `text` | `text` for 3-post thread, `image` for portrait PNG card |
| `SKIP_HISTORICAL` | `false` | Skip archive API calls (faster, no historical data) |
| `WEATHER_CACHE_TTL_MINUTES` | `30` | How long to cache weather lookups |
| `DB_PATH` | `data/zipwx.db` | SQLite database path |
| `INBOX_PATH` | `inbox` | Directory watched for YAML alert files |
| `LOG_PATH` | `logs/zipwx.log` | Log file path |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### Image mode dependencies

`POST_MODE=image` requires Pillow and matplotlib:

```bash
pip3 install Pillow matplotlib
```

If either is missing the bot logs a warning and falls back to text mode automatically.

## Architecture

```
Alert channels (inputs)          Notify channels (outputs)
─────────────────────            ─────────────────────────
FileWatcherAlertChannel    ──┐
FirehoseAlertChannel       ──┼──▶  ZipWx (orchestrator)  ──▶  BlueskyPostNotifyChannel
DMAlertChannel             ──┘         │                  ──▶  BlueskyDMNotifyChannel
                                       ▼
                                  WeatherService
                                  (Open-Meteo API, free)
                                       ▼
                                    Database
                                   (SQLite WAL)
```

**Adding a new alert channel** (e.g. webhooks):
1. Create `channels/alert/webhook.py` subclassing `AlertChannel`
2. Implement `start()` and `stop()`
3. Register it in `bot.py`'s `build_bot()`

**Adding a new notification channel** (e.g. email):
1. Create `channels/notify/email.py` subclassing `NotificationChannel`
2. Implement `send(payload) → NotificationResult`
3. Register it in `bot.py`'s `build_bot()`
4. Add routing logic in `ZipWx._route()`

## Project structure

```
bluesky_weather_bot/
├── main.py                          # entry point
├── bot.py                           # orchestrator (ZipWx + build_bot())
├── requirements.txt
├── .local.env.example               # copy to .local.env
│
├── bluesky_weather_bot/
│   ├── channels/
│   │   ├── alert/
│   │   │   ├── base.py              # AlertChannel ABC + AlertRequest dataclass
│   │   │   ├── file_watcher.py      # YAML inbox watcher
│   │   │   ├── firehose.py          # Bluesky public post stream
│   │   │   └── dm_poller.py         # Bluesky Direct Messages
│   │   └── notify/
│   │       ├── base.py              # NotificationChannel ABC + payload/result
│   │       ├── bluesky_post.py      # public reply (image or text thread)
│   │       └── bluesky_dm.py        # direct message reply (text only)
│   │
│   ├── weather/
│   │   ├── models.py                # dataclasses: WeatherReport, CurrentConditions, etc.
│   │   ├── resolver.py              # zip/city → lat/lon + timezone
│   │   ├── client.py                # Open-Meteo API client (no key required)
│   │   ├── formatter.py             # WeatherReport → Bluesky post strings (text mode)
│   │   ├── image_formatter.py       # WeatherReport → PNG image card (image mode)
│   │   └── service.py               # WeatherService facade (single entry point)
│   │
│   ├── storage/
│   │   └── db.py                    # SQLite: weather_cache, requests, responses
│   │
│   └── config/
│       └── settings.py              # loads .local.env → Settings dataclass
│
├── docs/                            # screenshots and assets
├── tests/                           # pytest suite
├── data/                            # SQLite database (gitignored)
├── inbox/                           # YAML alert files drop here
│   ├── archive/                     # processed files moved here
│   └── errors/                      # unparseable files moved here
└── logs/                            # log files (gitignored)
```

## Inbox YAML format

```yaml
full_message: 'Red Rocks Park 30-day rain total: 0.71 inches #RainData #COWx'
message: 'Red Rocks Park 30-day rain total: 0.71 inches'
created_at: '2025-02-19T17:30:07+07:00'
host: Test
tags:
  - COWx
  - Rain
mentions:
```

## Weather data

Powered by [Open-Meteo](https://open-meteo.com/) — free, no API key required.

- **Current**: temp, feels-like, humidity, wind, gusts, cloud cover, pressure, visibility
- **Forecast**: next 6 hours (temp, precip probability, wind, cloud cover)
- **Historical**: same date last year + 10-year climatological average

All values shown in dual units: °F (°C), mph (km/h), inches (mm).
