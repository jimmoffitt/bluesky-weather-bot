"""
Database layer for bluesky_weather_bot.

SQLite with WAL mode. Schema:
  requests        — every inbound alert/request, with full latency timestamps
  responses       — every outbound notification, with delivery timestamps
  weather_cache   — 1-hour TTL cache of WeatherReport JSON blobs
  seen_dm_ids     — DM message IDs already processed (survives restarts)
  file_inbox_log  — audit trail for file watcher activity

Views:
  latency_summary — per-request breakdown of receive / processing / delivery /
                    end-to-end latency in seconds

Retention policy (enforced by prune_old_records(), call daily):
  requests / responses  → purged after 90 days
  weather_cache         → purged when expires_at passes (1-hour TTL)
  seen_dm_ids           → kept forever (small table)
  file_inbox_log        → purged after 90 days

Latency timestamps (all ISO8601 UTC, set by the orchestrator bot.py):

  requests table:
    source_created_at       — when the Bluesky post/DM was originally created
                              (from the AT Protocol record; NULL for file alerts)
    ingested_at             — when the alert channel dispatched the AlertRequest
    processing_started_at   — when WeatherService.lookup() was called
    formatting_started_at   — when lookup returned; formatting is about to begin
    processing_finished_at  — when format_thread() returned, response ready to send
    is_slow                 — 1 if end-to-end latency exceeded SLOW_REQUEST_THRESHOLD_SEC

  responses table:
    delivery_started_at     — when NotificationChannel.send() was called
    delivery_finished_at    — when the Bluesky API confirmed receipt

Latency intervals (computed by the latency_summary view):
  receive_latency_sec     = ingested_at - source_created_at
  lookup_latency_sec      = formatting_started_at - processing_started_at
  formatting_latency_sec  = processing_finished_at - formatting_started_at
  delivery_latency_sec    = delivery_finished_at - delivery_started_at
  end_to_end_latency_sec  = delivery_finished_at - source_created_at

Postgres migration path:
  - Swap `import sqlite3` for `import psycopg2 as sqlite3`
  - Change placeholder `?` → `%s` throughout
  - Change `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`
  - Remove `check_same_thread=False` from connect()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS            = 1
RETENTION_DAYS             = 90
SLOW_REQUEST_THRESHOLD_SEC = 30    # end-to-end latency above this sets is_slow = 1
DEFAULT_DB_PATH            = Path("data/zipwx.db")


class Database:

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        logger.info("Database connected: %s", self.path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Database closed.")

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        assert self._conn
        self._conn.executescript("""
            -- --------------------------------------------------------
            -- requests: every inbound alert regardless of source
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS requests (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,

                source_channel          TEXT    NOT NULL,
                -- 'firehose' | 'dm' | 'file'

                requester_handle        TEXT,
                -- Bluesky handle for social channels; NULL for file alerts

                raw_location            TEXT,
                -- Exactly what the user typed, e.g. "Denver, CO" or "80501"

                resolved_location       TEXT,
                -- Canonical display name after lookup, e.g. "Denver, CO"

                resolved_lat            REAL,
                resolved_lon            REAL,
                -- Stored so repeated requests skip re-resolution

                raw_content             TEXT    NOT NULL,
                -- Full post text / DM text / YAML file message field

                status                  TEXT    NOT NULL DEFAULT 'pending',
                -- 'pending' | 'complete' | 'error'

                error_message           TEXT,
                -- Populated when status = 'error'

                -- ---- Latency timestamps (all ISO8601 UTC) ----

                source_created_at       TEXT,
                -- When the Bluesky post/DM was originally created.
                -- Comes from the AT Protocol record timestamp.
                -- NULL for file alerts.

                ingested_at             TEXT,
                -- When the alert channel dispatched the AlertRequest.
                -- receive_latency = ingested_at - source_created_at

                processing_started_at   TEXT,
                -- When the orchestrator called WeatherService.lookup().

                formatting_started_at   TEXT,
                -- When WeatherService.lookup() returned; formatting is about to begin.
                -- lookup_latency = formatting_started_at - processing_started_at

                processing_finished_at  TEXT,
                -- When format_thread() returned, response ready to send.
                -- formatting_latency = processing_finished_at - formatting_started_at

                is_slow                 INTEGER NOT NULL DEFAULT 0,
                -- 1 if end-to-end latency exceeded SLOW_REQUEST_THRESHOLD_SEC.

                source_uri              TEXT
                -- AT-URI of the triggering post (firehose) or NULL.
                -- Used for deduplication: same post must never be processed twice.
                -- DMs use seen_dm_ids instead; source_uri is NULL for DMs and files.
            );

            CREATE INDEX IF NOT EXISTS idx_requests_status
                ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_ingested
                ON requests(ingested_at);
            CREATE INDEX IF NOT EXISTS idx_requests_handle
                ON requests(requester_handle);
            CREATE INDEX IF NOT EXISTS idx_requests_slow
                ON requests(is_slow);

            -- --------------------------------------------------------
            -- responses: every outbound notification
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS responses (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id           INTEGER NOT NULL REFERENCES requests(id),

                notify_channel       TEXT    NOT NULL,
                -- 'bluesky_post' | 'bluesky_dm'

                post_index           INTEGER NOT NULL DEFAULT 0,
                -- 0 = root post, 1 = first reply, 2 = second reply

                message_text         TEXT    NOT NULL,

                post_uri             TEXT,
                -- AT-URI of the sent post; NULL for DMs or failed sends

                success              INTEGER NOT NULL DEFAULT 1,
                -- 1 = delivered, 0 = failed

                -- ---- Delivery latency timestamps (ISO8601 UTC) ----

                delivery_started_at  TEXT,
                -- When NotificationChannel.send() was called.

                delivery_finished_at TEXT
                -- When the Bluesky API returned successfully.
                -- delivery_latency = delivery_finished_at - delivery_started_at
            );

            CREATE INDEX IF NOT EXISTS idx_responses_request
                ON responses(request_id);
            CREATE INDEX IF NOT EXISTS idx_responses_channel
                ON responses(notify_channel);

            -- --------------------------------------------------------
            -- weather_cache: 1-hour TTL blobs of WeatherReport JSON
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS weather_cache (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key     TEXT    NOT NULL UNIQUE,
                -- '{lat:.2f}:{lon:.2f}:{YYYYMMDDHH}'

                display_name  TEXT    NOT NULL,
                lat           REAL    NOT NULL,
                lon           REAL    NOT NULL,
                report_json   TEXT    NOT NULL,
                fetched_at    TEXT    NOT NULL,
                expires_at    TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cache_key
                ON weather_cache(cache_key);
            CREATE INDEX IF NOT EXISTS idx_cache_expires
                ON weather_cache(expires_at);

            -- --------------------------------------------------------
            -- this_day_history_cache: ERA5 "on this day" yearly records
            -- Keyed by lat/lon/month/day; refreshed once per year.
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS this_day_history_cache (
                cache_key   TEXT    PRIMARY KEY,
                -- '{lat:.2f}:{lon:.2f}:{MM:02d}-{DD:02d}'
                data_json   TEXT    NOT NULL,
                fetched_at  TEXT    NOT NULL,
                expires_at  TEXT    NOT NULL
            );

            -- --------------------------------------------------------
            -- seen_dm_ids: DM messages already processed (kept forever)
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS seen_dm_ids (
                message_id  TEXT PRIMARY KEY,
                convo_id    TEXT NOT NULL,
                seen_at     TEXT NOT NULL
            );

            -- --------------------------------------------------------
            -- user_prefs: per-user display preferences set via DM
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_did      TEXT PRIMARY KEY,
                -- Bluesky DID (did:plc:...)

                handle        TEXT,
                -- Stored for display; not used as a key

                units         TEXT NOT NULL DEFAULT 'imperial',
                -- 'imperial': °F primary, mph, in
                -- 'metric':   °C primary, km/h, mm

                home_raw      TEXT,
                -- What the user typed, e.g. "Denver, CO" or "80501"

                home_display  TEXT,
                -- Resolved canonical name, e.g. "Denver, CO"

                home_lat      REAL,
                home_lon      REAL,

                updated_at    TEXT NOT NULL
            );

            -- --------------------------------------------------------
            -- file_inbox_log: audit trail for file watcher
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS file_inbox_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT    NOT NULL,
                received_at   TEXT    NOT NULL,
                processed_at  TEXT,
                outcome       TEXT    NOT NULL,
                -- 'dispatched' | 'parse_error' | 'skipped'

                request_id    INTEGER REFERENCES requests(id),
                error_detail  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_inbox_received
                ON file_inbox_log(received_at);
            CREATE INDEX IF NOT EXISTS idx_inbox_outcome
                ON file_inbox_log(outcome);

            -- --------------------------------------------------------
            -- latency_summary view
            --
            -- Computes all five latency intervals per completed request.
            -- JULIANDAY arithmetic gives fractional days; x86400 = seconds.
            -- NULLs propagate naturally: file alerts with no source_created_at
            -- will have NULL for receive_latency and end_to_end_latency.
            -- Only the root post (post_index = 0) is joined for delivery timing.
            -- --------------------------------------------------------
            CREATE VIEW IF NOT EXISTS latency_summary AS
            SELECT
                r.id                    AS request_id,
                r.source_channel,
                r.requester_handle,
                r.resolved_location,
                r.status,
                r.is_slow,
                r.source_created_at,
                r.ingested_at,
                r.processing_started_at,
                r.formatting_started_at,
                r.processing_finished_at,
                resp.delivery_started_at,
                resp.delivery_finished_at,

                -- Time between original post creation and bot ingestion
                ROUND(
                    (JULIANDAY(r.ingested_at) - JULIANDAY(r.source_created_at))
                    * 86400.0, 3
                ) AS receive_latency_sec,

                -- Time spent calling the weather API
                ROUND(
                    (JULIANDAY(r.formatting_started_at) - JULIANDAY(r.processing_started_at))
                    * 86400.0, 3
                ) AS lookup_latency_sec,

                -- Time spent rendering the weather report into post text
                ROUND(
                    (JULIANDAY(r.processing_finished_at) - JULIANDAY(r.formatting_started_at))
                    * 86400.0, 3
                ) AS formatting_latency_sec,

                -- Time for Bluesky API to accept the post
                ROUND(
                    (JULIANDAY(resp.delivery_finished_at) - JULIANDAY(resp.delivery_started_at))
                    * 86400.0, 3
                ) AS delivery_latency_sec,

                -- Full wall-clock time the user experienced
                ROUND(
                    (JULIANDAY(resp.delivery_finished_at) - JULIANDAY(r.source_created_at))
                    * 86400.0, 3
                ) AS end_to_end_latency_sec

            FROM requests r
            LEFT JOIN responses resp
                ON  resp.request_id = r.id
                AND resp.post_index  = 0;
        """)
        # Migrate existing databases that predate newer columns
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(requests)").fetchall()
        }
        if "formatting_started_at" not in existing:
            self._conn.execute(
                "ALTER TABLE requests ADD COLUMN formatting_started_at TEXT"
            )
            logger.info("[db] Migrated: added formatting_started_at column")
        if "source_uri" not in existing:
            self._conn.execute(
                "ALTER TABLE requests ADD COLUMN source_uri TEXT"
            )
            logger.info("[db] Migrated: added source_uri column")

        # Always ensure the dedup index exists — safe to run on every connect
        # because source_uri is now guaranteed to be present.
        self._conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_source_uri
                ON requests(source_uri)
                WHERE source_uri IS NOT NULL AND source_channel != 'dm'
        """)

        self._conn.commit()

    # ------------------------------------------------------------------
    # requests — core CRUD
    # ------------------------------------------------------------------

    def save_request(
        self,
        source_channel: str,
        raw_content: str,
        requester_handle: Optional[str] = None,
        raw_location: Optional[str] = None,
        resolved_location: Optional[str] = None,
        resolved_lat: Optional[float] = None,
        resolved_lon: Optional[float] = None,
        source_created_at: Optional[str] = None,
        ingested_at: Optional[str] = None,
        status: str = "pending",
        source_uri: Optional[str] = None,
    ) -> Optional[int]:
        """
        Insert a new request row. Returns the new row ID, or None if the
        source_uri already exists (duplicate — safe to drop silently).

        source_created_at: ISO8601 from the AT Protocol record. None for file alerts.
        ingested_at:       Defaults to now() if not provided.
        source_uri:        AT-URI of the triggering post. Enforces deduplication
                           for firehose posts so reconnects never double-process.
        """
        assert self._conn
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO requests (
                source_channel, requester_handle,
                raw_location, resolved_location, resolved_lat, resolved_lon,
                raw_content, status,
                source_created_at, ingested_at,
                source_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_channel, requester_handle,
                raw_location, resolved_location, resolved_lat, resolved_lon,
                raw_content, status,
                source_created_at,
                ingested_at or _now(),
                source_uri,
            ),
        )
        self._conn.commit()
        if cur.lastrowid == 0 or cur.rowcount == 0:
            return None
        return cur.lastrowid

    def update_request_resolved(
        self,
        request_id: int,
        resolved_location: str,
        resolved_lat: float,
        resolved_lon: float,
    ) -> None:
        assert self._conn
        self._conn.execute(
            """UPDATE requests
               SET resolved_location = ?, resolved_lat = ?, resolved_lon = ?
             WHERE id = ?""",
            (resolved_location, resolved_lat, resolved_lon, request_id),
        )
        self._conn.commit()

    def update_request_status(
        self,
        request_id: int,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        assert self._conn
        self._conn.execute(
            "UPDATE requests SET status = ?, error_message = ? WHERE id = ?",
            (status, error_message, request_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # requests — latency timestamps
    # Called by bot.py at each pipeline stage
    # ------------------------------------------------------------------

    def update_request_processing_start(self, request_id: int) -> None:
        """Call immediately before WeatherService.lookup()."""
        assert self._conn
        self._conn.execute(
            "UPDATE requests SET processing_started_at = ? WHERE id = ?",
            (_now(), request_id),
        )
        self._conn.commit()

    def update_request_formatting_start(self, request_id: int) -> None:
        """Call immediately after WeatherService.lookup() returns, before formatting."""
        assert self._conn
        self._conn.execute(
            "UPDATE requests SET formatting_started_at = ? WHERE id = ?",
            (_now(), request_id),
        )
        self._conn.commit()

    def update_request_processing_finish(self, request_id: int) -> None:
        """Call immediately after format_thread() returns."""
        assert self._conn
        self._conn.execute(
            "UPDATE requests SET processing_finished_at = ? WHERE id = ?",
            (_now(), request_id),
        )
        self._conn.commit()

    def update_request_mark_slow(
        self,
        request_id: int,
        delivery_finished_at: str,
        source_created_at: Optional[str],
    ) -> bool:
        """
        Computes end-to-end latency and sets is_slow = 1 if it exceeds
        SLOW_REQUEST_THRESHOLD_SEC. Returns True if flagged.

        Call in bot.py after recording delivery_finished_at.
        """
        if source_created_at is None:
            return False
        try:
            elapsed = (
                datetime.fromisoformat(delivery_finished_at) -
                datetime.fromisoformat(source_created_at)
            ).total_seconds()
        except (ValueError, TypeError):
            return False

        if elapsed > SLOW_REQUEST_THRESHOLD_SEC:
            assert self._conn
            self._conn.execute(
                "UPDATE requests SET is_slow = 1 WHERE id = ?", (request_id,)
            )
            self._conn.commit()
            logger.warning(
                "[db] Slow request %d: %.1fs end-to-end (threshold %ds)",
                request_id, elapsed, SLOW_REQUEST_THRESHOLD_SEC,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # requests — queries
    # ------------------------------------------------------------------

    def get_request(self, request_id: int) -> Optional[dict]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending_requests(self) -> list[dict]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status = 'pending' ORDER BY ingested_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_requests(self, limit: int = 50) -> list[dict]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM requests ORDER BY ingested_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_slow_requests(self, limit: int = 50) -> list[dict]:
        """Returns requests flagged as slow, most recent first."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE is_slow = 1 ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # responses
    # ------------------------------------------------------------------

    def save_response(
        self,
        request_id: int,
        notify_channel: str,
        message_text: str,
        post_index: int = 0,
        post_uri: Optional[str] = None,
        success: bool = True,
        delivery_started_at: Optional[str] = None,
        delivery_finished_at: Optional[str] = None,
    ) -> int:
        """
        Log a sent (or attempted) notification. Returns the new row ID.

        Pass delivery_started_at / delivery_finished_at as ISO8601 strings
        captured in bot.py around the channel.send() call.
        """
        assert self._conn
        cur = self._conn.execute(
            """
            INSERT INTO responses (
                request_id, notify_channel, post_index,
                message_text, post_uri, success,
                delivery_started_at, delivery_finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, notify_channel, post_index,
                message_text, post_uri, int(success),
                delivery_started_at, delivery_finished_at,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_responses_for_request(self, request_id: int) -> list[dict]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM responses WHERE request_id = ? ORDER BY post_index",
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # weather_cache
    # ------------------------------------------------------------------

    def get_cached_report(self, lat: float, lon: float) -> Optional[dict]:
        key = _cache_key(lat, lon)
        assert self._conn
        row = self._conn.execute(
            "SELECT report_json FROM weather_cache WHERE cache_key = ? AND expires_at > ?",
            (key, _now()),
        ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def save_cached_report(
        self, lat: float, lon: float, display_name: str, report_json: dict
    ) -> None:
        key    = _cache_key(lat, lon)
        now_dt = datetime.utcnow()
        expires = (now_dt + timedelta(hours=CACHE_TTL_HOURS)).isoformat()
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO weather_cache
                (cache_key, display_name, lat, lon, report_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                display_name = excluded.display_name,
                report_json  = excluded.report_json,
                fetched_at   = excluded.fetched_at,
                expires_at   = excluded.expires_at
            """,
            (key, display_name, round(lat, 3), round(lon, 3),
             json.dumps(report_json), now_dt.isoformat(), expires),
        )
        self._conn.commit()

    def purge_expired_cache(self) -> int:
        assert self._conn
        cur = self._conn.execute(
            "DELETE FROM weather_cache WHERE expires_at <= ?", (_now(),)
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # this_day_history_cache
    # ------------------------------------------------------------------

    def get_this_day_history(self, lat: float, lon: float, month: int, day: int) -> Optional[list]:
        assert self._conn
        key = f"{lat:.2f}:{lon:.2f}:{month:02d}-{day:02d}"
        row = self._conn.execute(
            "SELECT data_json FROM this_day_history_cache WHERE cache_key=? AND expires_at > ?",
            (key, _now()),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_this_day_history(
        self, lat: float, lon: float, month: int, day: int, records: list
    ) -> None:
        assert self._conn
        key      = f"{lat:.2f}:{lon:.2f}:{month:02d}-{day:02d}"
        now_dt   = datetime.utcnow()
        # Refresh once per year — expire on Jan 15 next year
        expires  = datetime(now_dt.year + 1, 1, 15).isoformat()
        self._conn.execute(
            """
            INSERT INTO this_day_history_cache (cache_key, data_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json  = excluded.data_json,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (key, json.dumps(records), now_dt.isoformat(), expires),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # seen_dm_ids
    # ------------------------------------------------------------------

    def is_dm_seen(self, message_id: str) -> bool:
        assert self._conn
        return self._conn.execute(
            "SELECT 1 FROM seen_dm_ids WHERE message_id = ?", (message_id,)
        ).fetchone() is not None

    def mark_dm_seen(self, message_id: str, convo_id: str) -> None:
        assert self._conn
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_dm_ids (message_id, convo_id, seen_at) VALUES (?,?,?)",
            (message_id, convo_id, _now()),
        )
        self._conn.commit()

    def count_seen_dms(self) -> int:
        assert self._conn
        return self._conn.execute("SELECT COUNT(*) FROM seen_dm_ids").fetchone()[0]

    # ------------------------------------------------------------------
    # user_prefs
    # ------------------------------------------------------------------

    def get_user_prefs(self, did: str) -> Optional[dict]:
        """Return the prefs row for did, or None if not yet saved."""
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM user_prefs WHERE user_did = ?", (did,)
        ).fetchone()
        return dict(row) if row else None

    def set_user_prefs(
        self,
        did: str,
        handle: Optional[str] = None,
        units: Optional[str] = None,
        home_raw: Optional[str] = None,
        home_display: Optional[str] = None,
        home_lat: Optional[float] = None,
        home_lon: Optional[float] = None,
    ) -> None:
        """
        Upsert preference columns for did.
        Only explicitly-passed keyword args are written; others keep
        their current (or default) values.
        """
        assert self._conn
        existing = self.get_user_prefs(did)
        if existing is None:
            self._conn.execute(
                """INSERT INTO user_prefs
                   (user_did, handle, units, home_raw, home_display,
                    home_lat, home_lon, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (did, handle, units or "imperial",
                 home_raw, home_display, home_lat, home_lon, _now()),
            )
        else:
            updates: dict = {"updated_at": _now()}
            if handle       is not None: updates["handle"]       = handle
            if units        is not None: updates["units"]        = units
            if home_raw     is not None: updates["home_raw"]     = home_raw
            if home_display is not None: updates["home_display"] = home_display
            if home_lat     is not None: updates["home_lat"]     = home_lat
            if home_lon     is not None: updates["home_lon"]     = home_lon
            cols = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [did]
            self._conn.execute(
                f"UPDATE user_prefs SET {cols} WHERE user_did = ?", vals
            )
        self._conn.commit()

    def clear_home(self, did: str) -> None:
        """Remove saved home location; leave other prefs intact."""
        assert self._conn
        self._conn.execute(
            """UPDATE user_prefs
               SET home_raw=NULL, home_display=NULL,
                   home_lat=NULL, home_lon=NULL, updated_at=?
               WHERE user_did = ?""",
            (_now(), did),
        )
        self._conn.commit()

    def reset_prefs(self, did: str) -> None:
        """Delete the entire prefs row, reverting to all defaults."""
        assert self._conn
        self._conn.execute(
            "DELETE FROM user_prefs WHERE user_did = ?", (did,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # file_inbox_log
    # ------------------------------------------------------------------

    def log_inbox_file(
        self,
        filename: str,
        outcome: str,
        request_id: Optional[int] = None,
        error_detail: Optional[str] = None,
    ) -> int:
        assert self._conn
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO file_inbox_log
               (filename, received_at, processed_at, outcome, request_id, error_detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (filename, now, now, outcome, request_id, error_detail),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_inbox_log(self, limit: int = 100) -> list[dict]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM file_inbox_log ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Latency queries
    # ------------------------------------------------------------------

    def get_latency_stats(self, channel: Optional[str] = None) -> dict:
        """
        Returns average latency statistics across completed requests,
        optionally filtered by source_channel.

        All values in seconds. NULLs (e.g. file alert receive latency)
        are excluded from averages automatically by SQLite AVG().

        Example return:
          {
            "channel": "firehose",
            "sample_size": 142,
            "avg_receive_latency_sec": 1.24,
            "avg_lookup_latency_sec": 2.91,
            "avg_formatting_latency_sec": 0.96,
            "avg_delivery_latency_sec": 0.61,
            "avg_end_to_end_latency_sec": 5.72,
            "p95_end_to_end_latency_sec": 12.4,
            "slow_request_count": 3,
            "slow_request_pct": 2.11,
          }
        """
        assert self._conn
        where  = "WHERE r.status = 'complete'"
        params: list = []
        if channel:
            where += " AND r.source_channel = ?"
            params.append(channel)

        row = self._conn.execute(
            f"""
            SELECT
                COUNT(*)                          AS n,
                AVG(ls.receive_latency_sec)       AS avg_receive,
                AVG(ls.lookup_latency_sec)        AS avg_lookup,
                AVG(ls.formatting_latency_sec)    AS avg_formatting,
                AVG(ls.delivery_latency_sec)      AS avg_delivery,
                AVG(ls.end_to_end_latency_sec)    AS avg_e2e,
                SUM(r.is_slow)                    AS slow_count
            FROM latency_summary ls
            JOIN requests r ON r.id = ls.request_id
            {where}
            """,
            params,
        ).fetchone()

        n          = row["n"] or 0
        slow_count = row["slow_count"] or 0

        return {
            "channel":                      channel or "all",
            "sample_size":                  n,
            "avg_receive_latency_sec":      _r(row["avg_receive"]),
            "avg_lookup_latency_sec":       _r(row["avg_lookup"]),
            "avg_formatting_latency_sec":   _r(row["avg_formatting"]),
            "avg_delivery_latency_sec":     _r(row["avg_delivery"]),
            "avg_end_to_end_latency_sec":   _r(row["avg_e2e"]),
            "p95_end_to_end_latency_sec":   self._compute_p95_e2e(where, params),
            "slow_request_count":           slow_count,
            "slow_request_pct":             _r(slow_count / n * 100) if n else 0.0,
        }

    def get_latency_stats_by_channel(self) -> list[dict]:
        """Returns get_latency_stats() broken down per source channel."""
        assert self._conn
        channels = [
            r[0] for r in self._conn.execute(
                "SELECT DISTINCT source_channel FROM requests"
            ).fetchall()
        ]
        return [self.get_latency_stats(ch) for ch in channels]

    def _compute_p95_e2e(self, where: str, params: list) -> Optional[float]:
        """Pulls all end-to-end values and returns the 95th percentile."""
        rows = self._conn.execute(
            f"""
            SELECT ls.end_to_end_latency_sec
            FROM latency_summary ls
            JOIN requests r ON r.id = ls.request_id
            {where}
            AND ls.end_to_end_latency_sec IS NOT NULL
            ORDER BY ls.end_to_end_latency_sec
            """,
            params,
        ).fetchall()
        if not rows:
            return None
        values = [r[0] for r in rows]
        return _r(values[max(0, int(len(values) * 0.95) - 1)])

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_old_records(self, retention_days: int = RETENTION_DAYS) -> dict[str, int]:
        """
        Deletes records older than retention_days.
        Call once daily from scripts/maintenance.py.
        Returns {table: rows_deleted}.
        """
        assert self._conn
        cutoff  = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        results: dict[str, int] = {}

        cur = self._conn.execute(
            "DELETE FROM responses WHERE request_id IN "
            "(SELECT id FROM requests WHERE ingested_at < ?)", (cutoff,)
        )
        results["responses"] = cur.rowcount

        cur = self._conn.execute(
            "DELETE FROM requests WHERE ingested_at < ?", (cutoff,)
        )
        results["requests"] = cur.rowcount

        cur = self._conn.execute(
            "DELETE FROM file_inbox_log WHERE received_at < ?", (cutoff,)
        )
        results["file_inbox_log"] = cur.rowcount

        results["weather_cache"] = self.purge_expired_cache()
        self._conn.commit()

        total = sum(results.values())
        if total:
            logger.info("Pruned %d rows: %s", total, results)
        return results

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Row counts + slow-request flag. Useful for health checks."""
        assert self._conn

        def count(table: str) -> int:
            return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        def count_where(table: str, clause: str, *args) -> int:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {clause}", args
            ).fetchone()[0]

        return {
            "requests_total":    count("requests"),
            "requests_pending":  count_where("requests", "status = ?", "pending"),
            "requests_complete": count_where("requests", "status = ?", "complete"),
            "requests_error":    count_where("requests", "status = ?", "error"),
            "requests_slow":     count_where("requests", "is_slow = ?", 1),
            "responses_total":   count("responses"),
            "cache_entries":     count("weather_cache"),
            "cache_live":        count_where("weather_cache", "expires_at > ?", _now()),
            "seen_dm_ids":       count("seen_dm_ids"),
            "inbox_log_entries": count("file_inbox_log"),
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


def _cache_key(lat: float, lon: float) -> str:
    hour = datetime.utcnow().strftime("%Y%m%d%H")
    return f"{round(lat, 2):.2f}:{round(lon, 2):.2f}:{hour}"


def _r(val) -> Optional[float]:
    return round(float(val), 3) if val is not None else None
