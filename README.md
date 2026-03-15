# bluesky_weather_bot

A modular Bluesky bot that responds to weather requests from public posts,
direct messages, and file-based alerts. Reply with a portrait image card
or a 3-post text thread — your choice.

## What it looks like

**Image mode** (`POST_MODE=image`) — a single portrait card posted as a reply:

<img src="docs/screenshot_image_card.png" width="300" alt="Weather image card for Denver, CO showing 58°F clear sky with humidity, wind, gusts, precip and pressure stats">

**Text mode** (`POST_MODE=text`) — a 3-post thread (also used for DM replies):

```
📍 New York, NY | Sat Mar 14 10:15 PM EDT
☀️ Clear sky
🌡 41°F (5°C) | Feels 32°F (-0°C)
💧 Humidity: 41%
💨 Wind: 9mph (14km/h) NNW | Gusts 18mph (29km/h)
🌧 Precip: 0.00in (0.0mm)
📊 Pressure: 1022hPa
---
⏱ Next 6 Hours — New York, NY
10PM: 41°F, ☁ 5%, 💧 0%, 💨 9mph
11PM: 39°F, ☁ 4%, 💧 0%, 💨 7mph
12AM: 37°F, ☁ 50%, 💧 0%, 💨 4mph
1AM: 35°F, ☁ 18%, 💧 0%, 💨 5mph
2AM: 34°F, ☁ 32%, 💧 1%, 💨 4mph
3AM: 34°F, ☁ 100%, 💧 1%, 💨 3mph
---
📅 Historical — New York, NY
Last year (Mar 14, 2025):
  Hi 55°F (13°C) / Lo 33°F (1°C) | Precip 0.00in
10-yr avg (Mar ±7d):
  Hi 57°F (14°C) / Lo 38°F (3°C) | Precip 0.14in
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
| Public post — mention + zip | `@zipwx.bsky.social 80501` |
| Public post — mention + city | `@zipwx.bsky.social Denver, CO` |
| Public post — mention + city only | `@zipwx.bsky.social Portland` *(returns results for all matches)* |
| Direct message — zip or city | `80501` or `Denver, CO` or `Portland` |
| Direct message — home location | *(no text needed — uses your saved home location)* |
| File inbox | Drop a YAML file into `inbox/` |

The bot replies to the original post (public) or sends a DM response. Image
mode is used for public replies; DMs always use the text thread format since
Bluesky DMs do not support image attachments.

## Personalizing via DM

Users can configure their experience by sending commands directly to the bot via
Bluesky DM. Changes are stored per-user and apply to all future responses on
any channel.

### Weather requests

Just send a location — no trigger word needed:

```
80501
Denver, CO
Portland
```

If you have a home location saved, send any DM with no location and the bot
replies with your home weather automatically.

### Setting a home location

```
set home Denver, CO
set home 80501
set home Portland
```

The bot validates the location before saving. Once set, a blank DM (or any
unrecognized message) will return weather for your home location.

To remove it:

```
clear home
```

### Display units

Both °F and °C (and mph/km/h, in/mm) are always shown. The units setting
controls which value appears first and is styled as the primary reading.

| Command | Effect |
|---------|--------|
| `imperial` | °F · mph · inches displayed first (default) |
| `metric` | °C · km/h · mm displayed first |

Aliases also accepted: `set units imperial`, `use imperial`, `fahrenheit`,
`set units metric`, `use metric`, `celsius`.

### Viewing and resetting preferences

| Command | Effect |
|---------|--------|
| `settings` | Show your current units and home location |
| `reset` | Clear all preferences and return to defaults |
| `help` | List all available commands |

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

## Running as a system service

On Linux (Raspberry Pi, Ubuntu, Debian), you can run the bot as a `systemd`
service so it starts automatically on boot and restarts on failure.

**1. Create the unit file:**

```bash
sudo nano /etc/systemd/system/weather-bot.service
```

```ini
[Unit]
Description=Bluesky Weather Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/bluesky-weather-bot
EnvironmentFile=/path/to/bluesky-weather-bot/.local.env
ExecStart=/path/to/bluesky-weather-bot/.venv/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_USER` and `/path/to/bluesky-weather-bot` with your actual
username and project directory.

**2. Enable and start:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable weather-bot
sudo systemctl start weather-bot
```

**3. Check status and logs:**

```bash
sudo systemctl status weather-bot
journalctl -u weather-bot -f       # follow live logs
journalctl -u weather-bot -n 100   # last 100 lines
```

**4. Stop or restart:**

```bash
sudo systemctl stop weather-bot
sudo systemctl restart weather-bot
```

> **Important:** `ExecStart` must point to the venv's `python3` directly
> (not `python` or the system Python) so all dependencies are available.
> See the note in [Quick start](#quick-start) about using the correct interpreter.

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

All values shown in dual units (°F/°C, mph/km/h, in/mm). Display order is
user-configurable via DM — see [Personalizing via DM](#personalizing-via-dm).
