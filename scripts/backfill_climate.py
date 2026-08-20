#!/usr/bin/env python3
"""
Backfill historical weather data into data/climate.db.

Resolves each location, fetches N years of daily data from the
Open-Meteo archive API, inserts into weather_daily, then computes
location_climate records and averages for every day-of-year.

Usage examples:
    # Zip codes, full history (1940-present, the default)
    python3 scripts/backfill_climate.py --zips 80501 80302 80203

    # City names, capped to the last 10 years instead
    python3 scripts/backfill_climate.py --cities "Denver, CO" "Boulder, CO" --years 10

    # Every city in archive/cities.py's TOP_200 list (~203 cities), full
    # history — 17k+ calls, well over the 10,000/day free-tier quota, so
    # this stops cleanly partway through and picks up where it left off
    # (already-fetched years are skipped) on the next run:
    python3 scripts/backfill_climate.py --top200
    python3 scripts/backfill_climate.py --top200   # next day, resumes

    # Refresh nightly (just yesterday for all known locations)
    python3 scripts/backfill_climate.py --yesterday

Options:
    --zips ZIPCODE ...        5-digit US zip codes
    --cities "City, ST" ...   City names (quote multi-word names)
    --top200                  All cities in archive/cities.py's TOP_200 list —
                               coordinates come straight from that table, no
                               geocoding needed. Combines with --zips/--cities.
    --years N                 Years of history to fetch, most recent N years.
                               Default: full archive back to 1940 (ERA5's
                               coverage start — uniform for every location).
    --max-calls N              Stop cleanly once this many API calls have
                               been made this run (default: 9500, just under
                               Open-Meteo's 10,000/day free-tier cap). Not an
                               error — re-run the same command to continue;
                               years already in the DB are skipped.
    --db   PATH               Path to climate.db (default: data/climate.db)
    --yesterday               Only fetch yesterday's data for all locations in DB
    --dry-run                 Resolve locations and print plan, no API calls
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from bluesky_weather_bot.archive.cities import TOP_200
from bluesky_weather_bot.storage.climate_db import ClimateDatabase, location_key as make_key
from bluesky_weather_bot.weather.archive_client import ArchiveClient
from bluesky_weather_bot.weather.resolver import LocationResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def resolve_locations(raw_list: list[str]) -> list[dict]:
    """
    Resolve a list of raw location strings (zips or city names) to dicts with
    keys: location_key, lat, lon, display_name, zip_code, timezone.

    Skips and logs any that fail to resolve.
    """
    resolver = LocationResolver()
    resolved: list[dict] = []
    seen_keys: set[str] = set()

    for raw in raw_list:
        try:
            locations = resolver.resolve(raw)
        except (ValueError, RuntimeError) as e:
            logger.warning("Could not resolve %r: %s", raw, e)
            continue

        for loc in locations:
            key = make_key(loc.lat, loc.lon)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            zip_code = raw.strip() if raw.strip().isdigit() and len(raw.strip()) == 5 else None

            resolved.append({
                "location_key": key,
                "lat":          loc.lat,
                "lon":          loc.lon,
                "display_name": loc.display_name,
                "zip_code":     zip_code,
                "timezone":     loc.timezone,
            })
            logger.info("Resolved %r → %s  (%s)", raw, loc.display_name, key)

    return resolved


def top200_locations() -> list[dict]:
    """
    Builds the resolved-location dict list (same shape as resolve_locations())
    directly from archive/cities.py's TOP_200 table — no geocoding calls
    needed since that table already has lat/lon/timezone/zip baked in.
    """
    resolved: list[dict] = []
    seen_keys: set[str] = set()

    for c in TOP_200:
        key = make_key(c.lat, c.lon)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        resolved.append({
            "location_key": key,
            "lat":          c.lat,
            "lon":          c.lon,
            "display_name": c.name,
            "zip_code":     c.zip_code,
            "timezone":     c.timezone,
        })

    return resolved


def backfill_location(
    cdb: ClimateDatabase,
    client: ArchiveClient,
    loc: dict,
    years: Optional[int],
    dry_run: bool = False,
    max_calls: Optional[int] = None,
) -> dict:
    """
    Fetch and store historical data for one location. Skips years already
    present in the DB (see get_years_present()) except the current
    calendar year, which is re-fetched every time since it's still
    in progress and may have grown since the last run.

    Returns a result summary dict.
    """
    key          = loc["location_key"]
    display_name = loc["display_name"]
    zip_code     = loc.get("zip_code")

    existing_start, existing_end = cdb.get_date_range(key)
    if existing_start:
        logger.info(
            "%s: existing data %s → %s",
            display_name, existing_start, existing_end,
        )

    if dry_run:
        span = f"{years} years" if years is not None else "full history (1940-present)"
        logger.info("[dry-run] Would fetch %s for %s", span, display_name)
        return {"location": display_name, "status": "dry-run", "rows": 0}

    skip_years = cdb.get_years_present(key) - {date.today().year}

    t0 = time.time()
    try:
        cdb.register_location(
            loc_key=key,
            lat=loc["lat"],
            lon=loc["lon"],
            display_name=display_name,
            zip_code=zip_code,
            timezone=loc.get("timezone"),
        )
        records = client.fetch_daily_years(
            lat=loc["lat"],
            lon=loc["lon"],
            years=years,
            location_key=key,
            display_name=display_name,
            zip_code=zip_code,
            timezone=loc["timezone"],
            skip_years=skip_years,
            max_calls=max_calls,
        )
        inserted = cdb.insert_daily_records(records)
        climate_rows = cdb.compute_climate_stats(key, zip_code=zip_code, display_name=display_name)
        elapsed = time.time() - t0

        logger.info(
            "✓ %-30s  %4d daily rows  %3d climate DOYs  %.1fs",
            display_name, inserted, climate_rows, elapsed,
        )
        return {
            "location":     display_name,
            "status":       "ok",
            "rows":         inserted,
            "climate_doys": climate_rows,
            "elapsed_sec":  round(elapsed, 1),
        }
    except Exception as e:
        logger.error("✗ %s: %s", display_name, e)
        return {"location": display_name, "status": "error", "error": str(e)}


def backfill_yesterday(cdb: ClimateDatabase, client: ArchiveClient, dry_run: bool) -> None:
    """Fetch yesterday's data for every location already in the DB."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    locations = cdb.list_locations()

    if not locations:
        logger.warning("No locations in climate.db yet. Run a full backfill first.")
        return

    logger.info("Updating %d locations with data for %s", len(locations), yesterday)

    for loc_row in locations:
        key = loc_row["location_key"]
        # Parse lat/lon back from the key  '40.17:-105.10'
        try:
            lat_s, lon_s = key.split(":")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            logger.warning("Cannot parse location_key %r, skipping", key)
            continue

        display_name = loc_row.get("display_name") or key
        zip_code     = loc_row.get("zip_code")

        if dry_run:
            logger.info("[dry-run] Would fetch %s for %s", yesterday, display_name)
            continue

        try:
            records = client.fetch_daily(
                lat=lat, lon=lon,
                start_date=yesterday, end_date=yesterday,
                location_key=key,
                display_name=display_name,
                zip_code=zip_code,
            )
            cdb.insert_daily_records(records)
            cdb.compute_climate_stats(key, zip_code=zip_code, display_name=display_name)
            logger.info("Updated %s for %s", yesterday, display_name)
        except Exception as e:
            logger.error("Failed to update %s: %s", display_name, e)


def main() -> None:
    """CLI entry point: parses args, resolves the requested zips/cities,
    fetches and inserts their historical daily records, computes
    climatological stats, and prints a summary."""
    parser = argparse.ArgumentParser(
        description="Backfill historical weather data into climate.db"
    )
    parser.add_argument("--zips",      nargs="+", metavar="ZIP",    default=[])
    parser.add_argument("--cities",    nargs="+", metavar="CITY",   default=[])
    parser.add_argument("--top200",    action="store_true",
                        help="Backfill every city in archive/cities.py's TOP_200 list")
    parser.add_argument("--years",     type=int,  default=None,     metavar="N",
                        help="Years of history, most recent N. Default: full archive to 1940.")
    parser.add_argument("--max-calls", type=int,  default=9500,     metavar="N",
                        help="Stop cleanly after this many API calls this run (default: 9500, "
                             "just under the 10,000/day free-tier quota). Re-run to resume.")
    parser.add_argument("--db",        default="data/climate.db",   metavar="PATH")
    parser.add_argument("--yesterday", action="store_true",
                        help="Only fetch yesterday's data for all known locations")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Resolve locations and print plan without API calls")
    args = parser.parse_args()

    db_path = Path(args.db)
    client  = ArchiveClient()

    with ClimateDatabase(db_path) as cdb:

        if args.yesterday:
            backfill_yesterday(cdb, client, dry_run=args.dry_run)
            print_stats(cdb)
            return

        raw_locations = args.zips + args.cities
        if not raw_locations and not args.top200:
            parser.error("Provide at least one --zips, --cities, or --top200.")

        locations = resolve_locations(raw_locations) if raw_locations else []
        if args.top200:
            seen_keys = {loc["location_key"] for loc in locations}
            for loc in top200_locations():
                if loc["location_key"] not in seen_keys:
                    seen_keys.add(loc["location_key"])
                    locations.append(loc)

        if not locations:
            logger.error("No locations resolved. Nothing to do.")
            sys.exit(1)

        span = f"{args.years} year(s)" if args.years is not None else "full history (1940-present)"
        print(f"\nBackfilling {len(locations)} location(s), {span} each\n")

        results = []
        quota_stopped = False
        for i, loc in enumerate(locations, 1):
            if client.call_count >= args.max_calls:
                remaining = len(locations) - i + 1
                print(
                    f"\nCall budget ({args.max_calls}) reached after {i - 1} location(s) — "
                    f"stopping cleanly. {remaining} location(s) remain.\n"
                    f"Re-run this exact command to continue — years already fetched are skipped."
                )
                quota_stopped = True
                break

            print(f"[{i}/{len(locations)}] {loc['display_name']}")
            result = backfill_location(
                cdb, client, loc,
                years=args.years,
                dry_run=args.dry_run,
                max_calls=args.max_calls,
            )
            results.append(result)

        # Summary
        ok    = sum(1 for r in results if r["status"] == "ok")
        errs  = sum(1 for r in results if r["status"] == "error")
        total = sum(r.get("rows", 0) for r in results)
        print(f"\n{'─'*50}")
        print(f"Done: {ok} succeeded, {errs} failed, {total:,} daily rows inserted"
              + (" (stopped early — quota reached)" if quota_stopped else ""))
        print(f"API calls made this run: {client.call_count}")

        if not args.dry_run:
            print_stats(cdb)


def print_stats(cdb: ClimateDatabase) -> None:
    """Prints climate.db's row/location/zip counts — the script's closing summary."""
    stats = cdb.get_stats()
    print(f"\nclimate.db stats:")
    print(f"  weather_daily rows:    {stats['weather_daily_rows']:>8,}")
    print(f"  location_climate rows: {stats['location_climate_rows']:>8,}")
    print(f"  distinct locations:    {stats['distinct_locations']:>8,}")
    print(f"  distinct zips:         {stats['distinct_zips']:>8,}")
    print(f"  date range:            {stats['earliest_date']} → {stats['latest_date']}")


if __name__ == "__main__":
    main()
