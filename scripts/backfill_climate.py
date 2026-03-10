#!/usr/bin/env python3
"""
Backfill historical weather data into data/climate.db.

Resolves each location, fetches N years of daily data from the
Open-Meteo archive API, inserts into weather_daily, then computes
location_climate records and averages for every day-of-year.

Usage examples:
    # Zip codes
    python3 scripts/backfill_climate.py --zips 80501 80302 80203

    # City names
    python3 scripts/backfill_climate.py --cities "Denver, CO" "Boulder, CO"

    # Mix, 10 years (default), custom db path
    python3 scripts/backfill_climate.py --zips 80501 --cities "Denver, CO" --years 10

    # Refresh nightly (just yesterday for all known locations)
    python3 scripts/backfill_climate.py --yesterday

Options:
    --zips ZIPCODE ...        5-digit US zip codes
    --cities "City, ST" ...   City names (quote multi-word names)
    --years N                 Years of history to fetch (default: 10)
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

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

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


def backfill_location(
    cdb: ClimateDatabase,
    client: ArchiveClient,
    loc: dict,
    years: int,
    dry_run: bool = False,
) -> dict:
    """
    Fetch and store historical data for one location.
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
        logger.info("[dry-run] Would fetch %d years for %s", years, display_name)
        return {"location": display_name, "status": "dry-run", "rows": 0}

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

        time.sleep(0.2)   # brief pause between locations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historical weather data into climate.db"
    )
    parser.add_argument("--zips",      nargs="+", metavar="ZIP",    default=[])
    parser.add_argument("--cities",    nargs="+", metavar="CITY",   default=[])
    parser.add_argument("--years",     type=int,  default=10,       metavar="N")
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
        if not raw_locations:
            parser.error("Provide at least one --zips or --cities location.")

        locations = resolve_locations(raw_locations)
        if not locations:
            logger.error("No locations resolved. Nothing to do.")
            sys.exit(1)

        print(f"\nBackfilling {len(locations)} location(s), {args.years} year(s) each\n")

        results = []
        for i, loc in enumerate(locations, 1):
            print(f"[{i}/{len(locations)}] {loc['display_name']}")
            result = backfill_location(
                cdb, client, loc,
                years=args.years,
                dry_run=args.dry_run,
            )
            results.append(result)
            # Brief pause between locations to be a good API citizen
            if i < len(locations):
                time.sleep(0.5)

        # Summary
        ok    = sum(1 for r in results if r["status"] == "ok")
        errs  = sum(1 for r in results if r["status"] == "error")
        total = sum(r.get("rows", 0) for r in results)
        print(f"\n{'─'*50}")
        print(f"Done: {ok} succeeded, {errs} failed, {total:,} daily rows inserted")

        if not args.dry_run:
            print_stats(cdb)


def print_stats(cdb: ClimateDatabase) -> None:
    stats = cdb.get_stats()
    print(f"\nclimate.db stats:")
    print(f"  weather_daily rows:    {stats['weather_daily_rows']:>8,}")
    print(f"  location_climate rows: {stats['location_climate_rows']:>8,}")
    print(f"  distinct locations:    {stats['distinct_locations']:>8,}")
    print(f"  distinct zips:         {stats['distinct_zips']:>8,}")
    print(f"  date range:            {stats['earliest_date']} → {stats['latest_date']}")


if __name__ == "__main__":
    main()
