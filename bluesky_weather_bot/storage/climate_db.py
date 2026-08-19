"""
Climate database — separate SQLite file (data/climate.db).

Two tables:
  weather_daily     — one row per location per date, raw daily observations
  location_climate  — precomputed records and climatological averages
                      per location × day-of-year (1–366)

Typical usage:
  with ClimateDatabase() as cdb:
      cdb.insert_daily_records(records)
      cdb.compute_climate_stats(location_key, zip_code, display_name)
      row = cdb.get_climate_for_doy(location_key, day_of_year)

Kept separate from the bot's operational zipwx.db because:
  - Different growth rate (bulk historical vs. per-request)
  - Different retention (kept forever vs. 90-day rolling)
  - Can be rebuilt from scratch via backfill script without touching bot data
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CLIMATE_DB_PATH = Path("data/climate.db")


class ClimateDatabase:

    def __init__(self, path: str | Path = DEFAULT_CLIMATE_DB_PATH) -> None:
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Opens the connection (WAL mode, larger page cache than the
        operational DB since backfill runs do bulk inserts) and ensures the
        schema exists."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
        self._create_schema()
        logger.info("ClimateDatabase connected: %s", self.path)

    def close(self) -> None:
        """Closes the connection. Safe to call even if never connected."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("ClimateDatabase closed.")

    def __enter__(self) -> "ClimateDatabase":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        """Creates weather_daily and location_climate if missing — idempotent, safe on every connect()."""
        assert self._conn
        self._conn.executescript("""
            -- --------------------------------------------------------
            -- weather_daily: raw daily observations per location
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS weather_daily (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                location_key        TEXT    NOT NULL,
                -- '{lat:.2f}:{lon:.2f}', e.g. '40.17:-105.10'

                zip_code            TEXT,
                -- 5-digit ZIP if request came from a zip lookup; else NULL

                display_name        TEXT,
                -- Human-readable name, e.g. 'Denver, CO'

                date                TEXT    NOT NULL,
                -- YYYY-MM-DD

                temp_max_f          REAL,
                temp_min_f          REAL,
                temp_mean_f         REAL,
                precipitation_in    REAL,
                -- liquid-equivalent inches (rain + melted snow)

                snowfall_in         REAL,
                -- snowfall in inches

                wind_speed_max_mph  REAL,
                weather_code        INTEGER,
                -- dominant WMO weather code for the day

                source              TEXT    NOT NULL DEFAULT 'open-meteo-archive',
                fetched_at          TEXT    NOT NULL,

                UNIQUE(location_key, date)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_location
                ON weather_daily(location_key);
            CREATE INDEX IF NOT EXISTS idx_daily_date
                ON weather_daily(date);
            CREATE INDEX IF NOT EXISTS idx_daily_zip
                ON weather_daily(zip_code);
            CREATE INDEX IF NOT EXISTS idx_daily_loc_date
                ON weather_daily(location_key, date);

            -- --------------------------------------------------------
            -- location_climate: precomputed records + averages
            -- One row per (location, day-of-year). Rebuilt from weather_daily.
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS location_climate (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                location_key        TEXT    NOT NULL,
                zip_code            TEXT,
                display_name        TEXT,
                day_of_year         INTEGER NOT NULL,
                -- 1–366 (day 366 only exists in leap years)

                -- All-time records
                record_high_f       REAL,
                record_high_date    TEXT,   -- YYYY-MM-DD

                record_low_f        REAL,
                record_low_date     TEXT,

                record_precip_in    REAL,
                record_precip_date  TEXT,

                record_snow_in      REAL,
                record_snow_date    TEXT,

                -- Climatological averages (mean over all years with data)
                avg_high_f          REAL,
                avg_low_f           REAL,
                avg_mean_f          REAL,
                avg_precip_in       REAL,
                avg_snow_in         REAL,

                years_of_data       INTEGER,
                -- number of distinct calendar years that contributed data

                last_computed       TEXT    NOT NULL,
                -- ISO8601 UTC timestamp of last recompute

                UNIQUE(location_key, day_of_year)
            );

            CREATE INDEX IF NOT EXISTS idx_climate_location
                ON location_climate(location_key);
            CREATE INDEX IF NOT EXISTS idx_climate_zip
                ON location_climate(zip_code);
            CREATE INDEX IF NOT EXISTS idx_climate_doy
                ON location_climate(day_of_year);

            -- --------------------------------------------------------
            -- locations: one row per distinct location ever stored.
            -- Explicit lat/lon columns enable fast nearest-neighbor queries.
            -- --------------------------------------------------------
            CREATE TABLE IF NOT EXISTS locations (
                location_key    TEXT    PRIMARY KEY,
                lat             REAL    NOT NULL,
                lon             REAL    NOT NULL,
                zip_code        TEXT,
                display_name    TEXT,
                timezone        TEXT,
                earliest_date   TEXT,   -- updated by compute_climate_stats
                latest_date     TEXT,
                added_at        TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_locations_zip
                ON locations(zip_code);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # locations — register & nearest-neighbor
    # ------------------------------------------------------------------

    def register_location(
        self,
        loc_key: str,
        lat: float,
        lon: float,
        display_name: Optional[str] = None,
        zip_code: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> None:
        """Upsert a location into the locations table."""
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO locations (location_key, lat, lon, zip_code, display_name, timezone, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_key) DO UPDATE SET
                zip_code     = COALESCE(excluded.zip_code,     zip_code),
                display_name = COALESCE(excluded.display_name, display_name),
                timezone     = COALESCE(excluded.timezone,     timezone)
            """,
            (loc_key, round(lat, 6), round(lon, 6), zip_code, display_name, timezone, _now()),
        )
        self._conn.commit()

    def find_nearest_location(
        self,
        lat: float,
        lon: float,
        max_km: float = 100.0,
    ) -> Optional[dict]:
        """
        Return the stored location nearest to (lat, lon), or None if nothing
        is within max_km.  Also returns 'distance_km' in the result dict.

        Uses Haversine distance computed in Python over all stored locations —
        fine for hundreds of locations; revisit if the table exceeds ~50 K rows.
        """
        assert self._conn
        rows = self._conn.execute("SELECT * FROM locations").fetchall()
        if not rows:
            return None

        best: Optional[dict] = None
        best_km = float("inf")
        for row in rows:
            km = _haversine_km(lat, lon, row["lat"], row["lon"])
            if km < best_km:
                best_km = km
                best    = dict(row)

        if best is not None and best_km <= max_km:
            best["distance_km"] = round(best_km, 2)
            return best
        return None

    # ------------------------------------------------------------------
    # weather_daily — writes
    # ------------------------------------------------------------------

    def insert_daily_records(self, records: list[dict]) -> int:
        """
        Bulk-insert daily observation rows. Silently skips exact duplicates
        (location_key + date already present). Returns number of rows inserted.

        Each dict must have keys matching the weather_daily columns.
        """
        if not records:
            return 0
        assert self._conn
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO weather_daily (
                location_key, zip_code, display_name, date,
                temp_max_f, temp_min_f, temp_mean_f,
                precipitation_in, snowfall_in, wind_speed_max_mph,
                weather_code, source, fetched_at
            ) VALUES (
                :location_key, :zip_code, :display_name, :date,
                :temp_max_f, :temp_min_f, :temp_mean_f,
                :precipitation_in, :snowfall_in, :wind_speed_max_mph,
                :weather_code, :source, :fetched_at
            )
            """,
            records,
        )
        self._conn.commit()
        # rowcount from executemany is unreliable for INSERT OR IGNORE;
        # return input count as an approximation
        return len(records)

    # ------------------------------------------------------------------
    # weather_daily — reads
    # ------------------------------------------------------------------

    def has_data_for_location(self, location_key: str) -> bool:
        """Checks whether any daily record exists for this location —
        lets the backfill script skip locations it's already populated."""
        assert self._conn
        row = self._conn.execute(
            "SELECT 1 FROM weather_daily WHERE location_key = ? LIMIT 1",
            (location_key,),
        ).fetchone()
        return row is not None

    def get_date_range(self, location_key: str) -> tuple[Optional[str], Optional[str]]:
        """Return (earliest_date, latest_date) for a location. Both YYYY-MM-DD or None."""
        assert self._conn
        row = self._conn.execute(
            "SELECT MIN(date) AS mn, MAX(date) AS mx FROM weather_daily WHERE location_key = ?",
            (location_key,),
        ).fetchone()
        return (row["mn"], row["mx"]) if row else (None, None)

    def get_daily_records(
        self,
        location_key: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch raw daily rows for a location, optionally filtered by date range."""
        assert self._conn
        sql = "SELECT * FROM weather_daily WHERE location_key = ?"
        params: list = [location_key]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY date"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_locations(self) -> list[dict]:
        """Return one summary row per distinct location in weather_daily."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                location_key,
                MAX(zip_code)      AS zip_code,
                MAX(display_name)  AS display_name,
                MIN(date)          AS earliest_date,
                MAX(date)          AS latest_date,
                COUNT(*)           AS row_count
            FROM weather_daily
            GROUP BY location_key
            ORDER BY display_name
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # location_climate — compute & write
    # ------------------------------------------------------------------

    def compute_climate_stats(
        self,
        location_key: str,
        zip_code: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> int:
        """
        Recompute location_climate rows for one location from weather_daily data.
        Upserts all 366 possible DOY rows. Returns count of rows written.

        This should be called:
          - After a backfill completes for a location
          - Nightly after yesterday's data is added
        """
        assert self._conn
        now = _now()

        # --- Step 1: aggregates per DOY via SQL ---
        agg_rows = self._conn.execute(
            """
            SELECT
                CAST(strftime('%j', date) AS INTEGER) AS doy,
                MAX(temp_max_f)        AS record_high_f,
                MIN(temp_min_f)        AS record_low_f,
                MAX(precipitation_in)  AS record_precip_in,
                MAX(snowfall_in)       AS record_snow_in,
                AVG(temp_max_f)        AS avg_high_f,
                AVG(temp_min_f)        AS avg_low_f,
                AVG(temp_mean_f)       AS avg_mean_f,
                AVG(precipitation_in)  AS avg_precip_in,
                AVG(snowfall_in)       AS avg_snow_in,
                COUNT(DISTINCT strftime('%Y', date)) AS years_of_data
            FROM weather_daily
            WHERE location_key = ?
            GROUP BY doy
            ORDER BY doy
            """,
            (location_key,),
        ).fetchall()

        if not agg_rows:
            logger.warning("No daily data found for location_key=%s", location_key)
            return 0

        # --- Step 2: find record dates (one extra query per DOY × field) ---
        # Done in a single pass with Python after fetching all candidates.
        all_rows = self._conn.execute(
            """
            SELECT
                date,
                CAST(strftime('%j', date) AS INTEGER) AS doy,
                temp_max_f, temp_min_f, precipitation_in, snowfall_in
            FROM weather_daily
            WHERE location_key = ?
            ORDER BY date
            """,
            (location_key,),
        ).fetchall()

        # Build per-DOY lookup: doy → first date where the record value occurred
        from collections import defaultdict
        doy_max_high:   dict[int, tuple] = {}  # doy → (value, date)
        doy_min_low:    dict[int, tuple] = {}
        doy_max_precip: dict[int, tuple] = {}
        doy_max_snow:   dict[int, tuple] = {}

        for r in all_rows:
            doy = r["doy"]
            if r["temp_max_f"] is not None:
                if doy not in doy_max_high or r["temp_max_f"] > doy_max_high[doy][0]:
                    doy_max_high[doy] = (r["temp_max_f"], r["date"])
            if r["temp_min_f"] is not None:
                if doy not in doy_min_low or r["temp_min_f"] < doy_min_low[doy][0]:
                    doy_min_low[doy] = (r["temp_min_f"], r["date"])
            if r["precipitation_in"] is not None:
                if doy not in doy_max_precip or r["precipitation_in"] > doy_max_precip[doy][0]:
                    doy_max_precip[doy] = (r["precipitation_in"], r["date"])
            if r["snowfall_in"] is not None:
                if doy not in doy_max_snow or r["snowfall_in"] > doy_max_snow[doy][0]:
                    doy_max_snow[doy] = (r["snowfall_in"], r["date"])

        # --- Step 3: upsert into location_climate ---
        count = 0
        for agg in agg_rows:
            doy = agg["doy"]
            self._conn.execute(
                """
                INSERT INTO location_climate (
                    location_key, zip_code, display_name, day_of_year,
                    record_high_f, record_high_date,
                    record_low_f,  record_low_date,
                    record_precip_in, record_precip_date,
                    record_snow_in,   record_snow_date,
                    avg_high_f, avg_low_f, avg_mean_f,
                    avg_precip_in, avg_snow_in,
                    years_of_data, last_computed
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(location_key, day_of_year) DO UPDATE SET
                    zip_code          = excluded.zip_code,
                    display_name      = excluded.display_name,
                    record_high_f     = excluded.record_high_f,
                    record_high_date  = excluded.record_high_date,
                    record_low_f      = excluded.record_low_f,
                    record_low_date   = excluded.record_low_date,
                    record_precip_in  = excluded.record_precip_in,
                    record_precip_date= excluded.record_precip_date,
                    record_snow_in    = excluded.record_snow_in,
                    record_snow_date  = excluded.record_snow_date,
                    avg_high_f        = excluded.avg_high_f,
                    avg_low_f         = excluded.avg_low_f,
                    avg_mean_f        = excluded.avg_mean_f,
                    avg_precip_in     = excluded.avg_precip_in,
                    avg_snow_in       = excluded.avg_snow_in,
                    years_of_data     = excluded.years_of_data,
                    last_computed     = excluded.last_computed
                """,
                (
                    location_key, zip_code, display_name, doy,
                    agg["record_high_f"],
                    doy_max_high.get(doy, (None, None))[1],
                    agg["record_low_f"],
                    doy_min_low.get(doy, (None, None))[1],
                    agg["record_precip_in"],
                    doy_max_precip.get(doy, (None, None))[1],
                    agg["record_snow_in"],
                    doy_max_snow.get(doy, (None, None))[1],
                    agg["avg_high_f"], agg["avg_low_f"], agg["avg_mean_f"],
                    agg["avg_precip_in"], agg["avg_snow_in"],
                    agg["years_of_data"], now,
                ),
            )
            count += 1

        # Update date range on the locations row
        date_range = self._conn.execute(
            "SELECT MIN(date), MAX(date) FROM weather_daily WHERE location_key = ?",
            (location_key,),
        ).fetchone()
        self._conn.execute(
            "UPDATE locations SET earliest_date = ?, latest_date = ? WHERE location_key = ?",
            (date_range[0], date_range[1], location_key),
        )
        self._conn.commit()
        logger.info(
            "Climate stats computed for %s: %d DOY rows (%s)",
            location_key, count, display_name or zip_code or "",
        )
        return count

    # ------------------------------------------------------------------
    # location_climate — reads
    # ------------------------------------------------------------------

    def get_climate_for_doy(
        self, location_key: str, day_of_year: int
    ) -> Optional[dict]:
        """Get precomputed climate row for one location + day-of-year."""
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM location_climate WHERE location_key = ? AND day_of_year = ?",
            (location_key, day_of_year),
        ).fetchone()
        return dict(row) if row else None

    def get_climate_for_location(self, location_key: str) -> list[dict]:
        """All 366 DOY rows for a location, ordered by day_of_year."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM location_climate WHERE location_key = ? ORDER BY day_of_year",
            (location_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_climate_by_zip(self, zip_code: str, day_of_year: int) -> Optional[dict]:
        """Look up climate row by zip code + DOY (convenience method)."""
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM location_climate WHERE zip_code = ? AND day_of_year = ?",
            (zip_code, day_of_year),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Summary counts (rows, distinct locations/zips) for the backfill
        script's progress reporting."""
        assert self._conn

        def count(table: str) -> int:
            """Row count for a whole table."""
            return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        locations = self._conn.execute(
            "SELECT COUNT(DISTINCT location_key) FROM weather_daily"
        ).fetchone()[0]
        zips = self._conn.execute(
            "SELECT COUNT(DISTINCT zip_code) FROM weather_daily WHERE zip_code IS NOT NULL"
        ).fetchone()[0]
        date_range = self._conn.execute(
            "SELECT MIN(date), MAX(date) FROM weather_daily"
        ).fetchone()

        return {
            "weather_daily_rows":    count("weather_daily"),
            "location_climate_rows": count("location_climate"),
            "distinct_locations":    locations,
            "distinct_zips":         zips,
            "earliest_date":         date_range[0],
            "latest_date":           date_range[1],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def location_key(lat: float, lon: float) -> str:
    """Canonical location key: '{lat:.2f}:{lon:.2f}'."""
    return f"{round(lat, 2):.2f}:{round(lon, 2):.2f}"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _now() -> str:
    """Current UTC time as an ISO8601 string."""
    return datetime.utcnow().isoformat()
