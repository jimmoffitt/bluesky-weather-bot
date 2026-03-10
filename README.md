# bluesky_weather_bot

A modular Bluesky bot that responds to weather requests from public posts,
direct messages, and file-based alerts.

## Quick start

```bash
# 1. Clone and set up environment
cd ~/projects/bluesky_weather_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure credentials
cp .local.env.example .local.env
# Edit .local.env with your Bluesky handle and app password

# 3. Run
python main.py
```

## How it works

Users trigger the bot by:
- Posting `#ZipWx Denver, CO` or `#ZipWx 80501` on Bluesky
- Sending a DM with a location
- Dropping a YAML file into the configured inbox folder

The bot replies with a 3-post thread: current conditions, 6-hour forecast,
and a historical comparison (year-ago + 10-year average) — all in dual units
(°F/°C, mph/km/h, in/mm).

## Architecture

```
Alert channels (inputs)          Notify channels (outputs)
─────────────────────            ─────────────────────────
FileWatcherAlertChannel    ──┐
FirehoseAlertChannel       ──┼──▶  ZipWx (orchestrator)  ──▶  BlueskyPostNotifyChannel
DMAlertChannel             ──┘         │                  ──▶  BlueskyDMNotifyChannel
                                       ▼
                                  WeatherService
                                  (Open-Meteo API)
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
│   │       ├── bluesky_post.py      # public reply thread
│   │       └── bluesky_dm.py        # direct message reply
│   │
│   ├── weather/
│   │   ├── models.py                # dataclasses: WeatherReport, CurrentConditions, etc.
│   │   ├── resolver.py              # zip/city → lat/lon + timezone
│   │   ├── client.py                # Open-Meteo API client
│   │   ├── formatter.py             # WeatherReport → Bluesky post strings
│   │   └── service.py               # WeatherService facade (single entry point)
│   │
│   ├── storage/
│   │   └── db.py                    # SQLite: weather_cache, requests, responses
│   │
│   └── config/
│       └── settings.py              # loads .local.env → Settings dataclass
│
├── tests/                           # pytest test stubs
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
