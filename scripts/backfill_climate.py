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

    # Every city in archive/cities.py's TOP_200 list (~203 cities, plus
    # Miami). Default depth is tiered, not a flat --years for everyone:
    # DEEP_HISTORY_NAMES (NYC, LA, Denver, SF, Minneapolis, Chicago, Miami)
    # get the full 1940-present archive; every other city gets only
    # SHALLOW_HISTORY_START_YEAR-present (2000-) to keep total call volume
    # down. Pass an explicit --years to override this uniformly for every
    # location instead. Requests are paced to stay within ArchiveClient's
    # DAILY_CALL_BUDGET of Open-Meteo's weighted daily quota, so a full run
    # takes weeks, unattended. If it's stopped, re-running picks up where
    # it left off (already-fetched years are skipped):
    python3 scripts/backfill_climate.py --top200

    # Catch up every known location to yesterday — run this after the
    # initial --top200 backfill finishes (locations may have finished on
    # different days) or periodically afterward to keep the archive
    # current. Fills each location's actual gap in one call, not just
    # a fixed single day:
    python3 scripts/backfill_climate.py --catchup

    # Fill in the non-core variables (see archive_client.EXTRA_FIELD_GROUPS)
    # for years already stored. Updates existing rows in place; never adds
    # rows or touches the core 10. Defaults to every group, every stored
    # location, biggest cities first. --dry-run prints the cost estimate:
    python3 scripts/backfill_climate.py --fill-extras --dry-run
    python3 scripts/backfill_climate.py --fill-extras humidity pressure
    python3 scripts/backfill_climate.py --fill-extras --cities "Denver, CO"

Options:
    --zips ZIPCODE ...        5-digit US zip codes
    --cities "City, ST" ...   City names (quote multi-word names)
    --top200                  All cities in archive/cities.py's TOP_200 list —
                               coordinates come straight from that table, no
                               geocoding needed. Combines with --zips/--cities.
    --years N                 Years of history to fetch, most recent N years.
                               Default: full archive back to 1940 (ERA5's
                               coverage start — uniform for every location).
    --max-calls N              Stop cleanly once this many API requests have
                               been made this run (default: 9500). A whole-run
                               cap, not the daily quota guard — that's the
                               budget pacing in ArchiveClient. Not an error —
                               re-run to continue; fetched years are skipped.
    --db   PATH               Path to climate.db (default: data/climate.db)
    --catchup                 Fetch each known location's gap between its
                               latest stored date and yesterday (one call
                               per location) — the lightweight keep-current
                               operation, not the heavy one-time archive pull.
    --fill-extras [GROUP ...] Fill non-core variables for years already in the
                               DB, by group (default: all groups). Combines
                               with --zips/--cities/--top200 to limit which
                               locations; otherwise every stored location.
    --dry-run                 Resolve locations and print plan, no API calls

Only one non-dry-run backfill may run at a time (they'd share, and
together overspend, Open-Meteo's daily quota) — enforced with a lock file
next to the database.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from bluesky_weather_bot.archive.cities import TOP_200
from bluesky_weather_bot.storage.climate_db import ClimateDatabase, location_key as make_key
from bluesky_weather_bot.weather.archive_client import (
    API_TO_COLUMN, DAILY_CALL_BUDGET, EXTRA_FIELD_GROUPS, ArchiveClient,
    pack_fields, request_weight,
)
from bluesky_weather_bot.weather.resolver import LocationResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Full 1940-present history is expensive (86 years/location) and every
# extra location multiplies the odds of tripping Open-Meteo's undocumented
# throttling. Only a handful of flagship cities get the full depth; the
# rest of TOP_200 gets a much cheaper recent-decades slice. Adjust freely —
# this is just a call-volume dial, not a data-availability limit.
DEEP_HISTORY_NAMES = {
    "New York, NY", "Los Angeles, CA", "Denver, CO", "San Francisco, CA",
    "Minneapolis, MN", "Chicago, IL", "Miami, FL",
}
SHALLOW_HISTORY_START_YEAR = 2000

# Miami isn't in TOP_200 (city-proper population ranks it outside the top
# 200; the metro area is far larger) but was explicitly requested as one
# of the deep-history cities, so it's added as a one-off extra location.
_MIAMI = {
    "location_key": make_key(25.7617, -80.1918),
    "lat":          25.7617,
    "lon":          -80.1918,
    "display_name": "Miami, FL",
    "zip_code":     "33130",
    "timezone":     "America/New_York",
}


def years_for(loc: dict, requested_years: Optional[int]) -> Optional[int]:
    """
    Per-location years-of-history depth. An explicit --years flag overrides
    everything uniformly (unchanged behavior); otherwise DEEP_HISTORY_NAMES
    get the full 1940-present archive and everything else is capped to
    SHALLOW_HISTORY_START_YEAR-present.
    """
    if requested_years is not None:
        return requested_years
    if loc["display_name"] in DEEP_HISTORY_NAMES:
        return None
    return date.today().year - SHALLOW_HISTORY_START_YEAR


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
        # Inserted per-year as each one is fetched (not batched to the
        # end) — a location stuck behind adaptive backoff after repeated
        # 429s can take a long time; this way a kill/crash mid-location
        # only loses whatever year was in flight, not everything already
        # fetched for it.
        inserted = 0

        def _save_year(records: list[dict]) -> None:
            nonlocal inserted
            inserted += cdb.insert_daily_records(records)

        client.fetch_daily_years(
            lat=loc["lat"],
            lon=loc["lon"],
            years=years,
            location_key=key,
            display_name=display_name,
            zip_code=zip_code,
            timezone=loc["timezone"],
            skip_years=skip_years,
            max_calls=max_calls,
            on_year_complete=_save_year,
        )
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


def backfill_catchup(cdb: ClimateDatabase, client: ArchiveClient, dry_run: bool) -> None:
    """
    Fills each known location's actual gap — from the day after its
    latest stored date through yesterday — not just a fixed single day.

    A location whose data ends several days ago (e.g. because a
    multi-day full backfill run finished on different days for different
    locations) catches all the way up in one range request; a location
    already current (latest_date == yesterday) is a no-op. One API call
    per location regardless of gap size — this is the lightweight
    keep-current operation, meant to run after the initial backfill (or
    periodically afterward), not the heavy one-time archive pull.
    """
    yesterday = date.today() - timedelta(days=1)
    locations = cdb.list_locations()

    if not locations:
        logger.warning("No locations in climate.db yet. Run a full backfill first.")
        return

    logger.info("Checking %d location(s) for catch-up through %s", len(locations), yesterday)

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

        _, existing_end = cdb.get_date_range(key)
        start = date.fromisoformat(existing_end) + timedelta(days=1) if existing_end else yesterday

        if start > yesterday:
            logger.info("%-30s already current (latest=%s)", display_name, existing_end)
            continue

        gap_days = (yesterday - start).days + 1

        if dry_run:
            logger.info("[dry-run] Would fetch %s: %s → %s (%d day(s))",
                        display_name, start, yesterday, gap_days)
            continue

        try:
            records = client.fetch_daily(
                lat=lat, lon=lon,
                start_date=start.isoformat(), end_date=yesterday.isoformat(),
                location_key=key,
                display_name=display_name,
                zip_code=zip_code,
            )
            inserted = cdb.insert_daily_records(records)
            cdb.compute_climate_stats(key, zip_code=zip_code, display_name=display_name)
            logger.info("Caught up %-30s %s → %s  (%d day(s), %d rows)",
                        display_name, start, yesterday, gap_days, inserted)
        except Exception as e:
            logger.error("Failed to catch up %s: %s", display_name, e)


def fill_extras_locations(cdb: ClimateDatabase, selected: list[dict]) -> list[dict]:
    """
    Locations for --fill-extras: the explicitly selected ones if any were
    given, otherwise every stored location, in TOP_200 (population) order
    with Miami and any other stored locations after.
    """
    if selected:
        return selected
    stored = {r["location_key"]: r for r in cdb.list_locations()}
    ordered = [loc for loc in top200_locations() + [_MIAMI] if loc["location_key"] in stored]
    seen = {loc["location_key"] for loc in ordered}
    for key, row in stored.items():
        if key in seen:
            continue
        try:
            lat_s, lon_s = key.split(":")
        except ValueError:
            logger.warning("Cannot parse location_key %r, skipping", key)
            continue
        ordered.append({
            "location_key": key, "lat": float(lat_s), "lon": float(lon_s),
            "display_name": row.get("display_name") or key,
            "zip_code": row.get("zip_code"), "timezone": "auto",
        })
    return ordered


def backfill_fill_extras(
    cdb: ClimateDatabase,
    client: ArchiveClient,
    locations: list[dict],
    groups: list[str],
    dry_run: bool,
    max_calls: Optional[int],
) -> None:
    """
    Fetches only the selected groups' variables, for location-years that
    still lack them (see ClimateDatabase.get_unfilled_years), and writes
    them into the existing rows. Each location is finished before the
    next; years go newest first. Fields are packed into requests of up to
    10 variables, since that's the billing minimum per request.

    A failed request is logged and left unrecorded, so the next run
    retries it. A successful one is recorded in field_fills even if the
    API returned nulls, so unavailable data isn't re-requested forever.
    """
    api_fields = [f for g in groups for f in EXTRA_FIELD_GROUPS[g]]
    columns    = [API_TO_COLUMN[f] for f in api_fields]
    yesterday  = date.today() - timedelta(days=1)

    plan_requests = 0
    plan_weight   = 0.0
    filled_rows   = 0

    for i, loc in enumerate(locations, 1):
        key  = loc["location_key"]
        name = loc["display_name"]
        needed = cdb.get_unfilled_years(key, columns)
        if not needed:
            logger.info("[%d/%d] %-30s nothing to fill", i, len(locations), name)
            continue

        loc_requests = 0
        for year in sorted(needed, reverse=True):
            year_fields = [f for f in api_fields if API_TO_COLUMN[f] in needed[year]]
            start = date(year, 1, 1)
            end   = min(date(year, 12, 31), yesterday)
            n_days = (end - start).days + 1

            for batch in pack_fields(year_fields):
                plan_requests += 1
                loc_requests  += 1
                plan_weight   += request_weight(len(batch), n_days)
                if dry_run:
                    continue
                if max_calls is not None and client.call_count >= max_calls:
                    logger.info("Call budget (%d) reached — stopping cleanly; re-run to resume", max_calls)
                    return
                batch_cols = [API_TO_COLUMN[f] for f in batch]
                try:
                    records = client.fetch_daily(
                        lat=loc["lat"], lon=loc["lon"],
                        start_date=start.isoformat(), end_date=end.isoformat(),
                        location_key=key, display_name=name,
                        zip_code=loc.get("zip_code"),
                        timezone=loc.get("timezone") or "auto",
                        fields=batch,
                    )
                except Exception as exc:
                    logger.warning("Failed to fill %s %d (%s): %s", name, year, ", ".join(batch), exc)
                    continue
                updated = cdb.update_daily_fields(records, batch_cols)
                filled_rows += updated
                through = max((r["date"] for r in records), default=end.isoformat())
                cdb.record_field_fill(key, year, batch_cols, through)
                logger.info("Filled %s %d: %d vars x %d rows", name, year, len(batch), updated)

        logger.info("[%d/%d] %-30s %d request(s) %s", i, len(locations), name,
                    loc_requests, "planned" if dry_run else "done")

    days = plan_weight / DAILY_CALL_BUDGET
    print(f"\n{'─'*50}")
    print(f"Fill extras ({', '.join(groups)}): {len(api_fields)} variable(s), "
          f"{len(locations)} location(s)")
    if dry_run:
        print(f"Would make {plan_requests:,} request(s) ≈ {plan_weight:,.0f} weighted calls "
              f"≈ {days:.1f} day(s) at the {DAILY_CALL_BUDGET:,}/day budget")
    else:
        print(f"Filled {filled_rows:,} row(s); {client.call_count} request(s) "
              f"(~{client.weighted_calls:,.0f} weighted calls)")


def acquire_backfill_lock(db_path: Path):
    """
    Takes an exclusive, non-blocking lock next to the DB so two backfills
    can't run at once and together overspend the shared daily quota.
    Exits with an error if another run holds it. The lock is released
    automatically when the process exits, however it exits.
    """
    lock_path = db_path.with_name(db_path.name + ".backfill.lock")
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another backfill is already running (lock held on {lock_path}).", file=sys.stderr)
        sys.exit(2)
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle  # keep a reference: closing it releases the lock


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
                        help="Stop cleanly after this many API requests this run (default: 9500). "
                             "Daily-quota pacing is automatic. Re-run to resume.")
    parser.add_argument("--db",        default="data/climate.db",   metavar="PATH")
    parser.add_argument("--catchup",   action="store_true",
                        help="Fetch each known location's gap through yesterday "
                             "(one call per location) instead of a full backfill")
    parser.add_argument("--fill-extras", nargs="*", metavar="GROUP", default=None,
                        choices=sorted(EXTRA_FIELD_GROUPS),
                        help="Fill non-core variables for years already stored, by group "
                             f"({', '.join(EXTRA_FIELD_GROUPS)}; default: all)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Resolve locations and print plan without API calls")
    args = parser.parse_args()

    db_path = Path(args.db)
    client  = ArchiveClient()
    lock = None if args.dry_run else acquire_backfill_lock(db_path)  # noqa: F841 — held for process lifetime

    with ClimateDatabase(db_path) as cdb:

        if args.fill_extras is not None:
            groups = args.fill_extras or list(EXTRA_FIELD_GROUPS)
            selected = resolve_locations(args.zips + args.cities) if (args.zips or args.cities) else []
            if args.top200:
                seen = {loc["location_key"] for loc in selected}
                selected += [loc for loc in top200_locations() + [_MIAMI] if loc["location_key"] not in seen]
            backfill_fill_extras(
                cdb, client, fill_extras_locations(cdb, selected), groups,
                dry_run=args.dry_run, max_calls=args.max_calls,
            )
            return

        if args.catchup:
            backfill_catchup(cdb, client, dry_run=args.dry_run)
            print_stats(cdb)
            return

        raw_locations = args.zips + args.cities
        if not raw_locations and not args.top200:
            parser.error("Provide at least one --zips, --cities, or --top200.")

        locations = resolve_locations(raw_locations) if raw_locations else []
        if args.top200:
            seen_keys = {loc["location_key"] for loc in locations}
            for loc in top200_locations() + [_MIAMI]:
                if loc["location_key"] not in seen_keys:
                    seen_keys.add(loc["location_key"])
                    locations.append(loc)

        if not locations:
            logger.error("No locations resolved. Nothing to do.")
            sys.exit(1)

        if args.years is None and args.top200:
            n_deep = sum(1 for loc in locations if loc["display_name"] in DEEP_HISTORY_NAMES)
            print(
                f"\nBackfilling {len(locations)} location(s): "
                f"{n_deep} full history (1940-present), "
                f"{len(locations) - n_deep} back to {SHALLOW_HISTORY_START_YEAR}\n"
            )
        else:
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
                years=years_for(loc, args.years),
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
        print(f"API requests made this run: {client.call_count} "
              f"(~{client.weighted_calls:,.0f} weighted calls against the quota)")

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
