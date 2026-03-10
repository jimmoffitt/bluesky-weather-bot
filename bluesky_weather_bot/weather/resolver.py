"""
Location resolver: converts raw user input (zip code, city name, or lat/lon)
into one or more ResolvedLocation objects with lat/lon and timezone.

Resolution order for string input:
  1. 5-digit zip code → pgeocode offline lookup
  2. "City, ST" / "City ST" / "City State" → TOP_CITIES table
  3. Ambiguous city (e.g. "Portland") without state → return all candidates
  4. Fallback: Nominatim geocoding (requires network)

For lat/lon input use resolve_latlon(lat, lon):
  1. timezonefinder for exact timezone
  2. Nearest entry in TOP_CITIES (offline, within 50 km)
  3. Fallback: Nominatim reverse geocoding

Dependencies: pip install pgeocode geopy timezonefinder
"""

from __future__ import annotations

import math
import re
import logging
from typing import Optional

from bluesky_weather_bot.weather.models import ResolvedLocation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ambiguous cities — require a state to be unambiguous
# ---------------------------------------------------------------------------

AMBIGUOUS_CITIES: dict[str, list[tuple[str, float, float, str]]] = {
    # key: city name (lower)  value: list of (display_name, lat, lon, timezone)
    "portland":    [("Portland, OR",     45.5051, -122.6750, "America/Los_Angeles"),
                    ("Portland, ME",     43.6591,  -70.2568, "America/New_York")],
    "springfield": [("Springfield, IL",  39.7817,  -89.6501, "America/Chicago"),
                    ("Springfield, MO",  37.2090,  -93.2923, "America/Chicago"),
                    ("Springfield, MA",  42.1015,  -72.5898, "America/New_York"),
                    ("Springfield, OH",  39.9242,  -83.8088, "America/New_York")],
    "columbus":    [("Columbus, OH",     39.9612,  -82.9988, "America/New_York"),
                    ("Columbus, GA",     32.4610,  -84.9877, "America/New_York")],
    "richmond":    [("Richmond, VA",     37.5407,  -77.4360, "America/New_York"),
                    ("Richmond, CA",     37.9358, -122.3478, "America/Los_Angeles")],
    "rochester":   [("Rochester, NY",    43.1566,  -77.6088, "America/New_York"),
                    ("Rochester, MN",    44.0234,  -92.4695, "America/Chicago")],
    "aurora":      [("Aurora, CO",       39.7294, -104.8319, "America/Denver"),
                    ("Aurora, IL",       41.7606,  -88.3201, "America/Chicago")],
    "manchester":  [("Manchester, NH",   42.9956,  -71.4548, "America/New_York"),
                    ("Manchester, TN",   35.4820,  -86.0886, "America/Chicago")],
    "florence":    [("Florence, SC",     34.1954,  -79.7626, "America/New_York"),
                    ("Florence, AL",     34.7998,  -87.6773, "America/Chicago")],
    "miami":       [("Miami, FL",        25.7617,  -80.1918, "America/New_York"),
                    ("Miami, OK",        36.8742,  -94.8769, "America/Chicago")],
}

# ---------------------------------------------------------------------------
# Top US cities lookup table
# Format: normalized_key → (display_name, lat, lon, timezone)
# ---------------------------------------------------------------------------

TOP_CITIES: dict[str, tuple[str, float, float, str]] = {
    # New York
    "new york, ny":       ("New York, NY",       40.7128,  -74.0060, "America/New_York"),
    "new york city, ny":  ("New York, NY",       40.7128,  -74.0060, "America/New_York"),
    "nyc":                ("New York, NY",       40.7128,  -74.0060, "America/New_York"),
    "brooklyn, ny":       ("Brooklyn, NY",       40.6782,  -73.9442, "America/New_York"),
    "buffalo, ny":        ("Buffalo, NY",        42.8864,  -78.8784, "America/New_York"),
    "rochester, ny":      ("Rochester, NY",      43.1566,  -77.6088, "America/New_York"),
    "yonkers, ny":        ("Yonkers, NY",        40.9312,  -73.8988, "America/New_York"),
    # Los Angeles
    "los angeles, ca":    ("Los Angeles, CA",    34.0522, -118.2437, "America/Los_Angeles"),
    "la, ca":             ("Los Angeles, CA",    34.0522, -118.2437, "America/Los_Angeles"),
    "long beach, ca":     ("Long Beach, CA",     33.7701, -118.1937, "America/Los_Angeles"),
    "anaheim, ca":        ("Anaheim, CA",        33.8353, -117.9145, "America/Los_Angeles"),
    "santa ana, ca":      ("Santa Ana, CA",      33.7455, -117.8677, "America/Los_Angeles"),
    "irvine, ca":         ("Irvine, CA",         33.6846, -117.8265, "America/Los_Angeles"),
    "san diego, ca":      ("San Diego, CA",      32.7157, -117.1611, "America/Los_Angeles"),
    # Bay Area
    "san francisco, ca":  ("San Francisco, CA",  37.7749, -122.4194, "America/Los_Angeles"),
    "sf, ca":             ("San Francisco, CA",  37.7749, -122.4194, "America/Los_Angeles"),
    "san jose, ca":       ("San Jose, CA",       37.3382, -121.8863, "America/Los_Angeles"),
    "oakland, ca":        ("Oakland, CA",        37.8044, -122.2711, "America/Los_Angeles"),
    "fremont, ca":        ("Fremont, CA",        37.5485, -121.9886, "America/Los_Angeles"),
    # Pacific NW
    "seattle, wa":        ("Seattle, WA",        47.6062, -122.3321, "America/Los_Angeles"),
    "spokane, wa":        ("Spokane, WA",        47.6588, -117.4260, "America/Los_Angeles"),
    "portland, or":       ("Portland, OR",       45.5051, -122.6750, "America/Los_Angeles"),
    "portland, me":       ("Portland, ME",       43.6591,  -70.2568, "America/New_York"),
    # Colorado
    "denver, co":         ("Denver, CO",         39.7392, -104.9903, "America/Denver"),
    "colorado springs, co": ("Colorado Springs, CO", 38.8339, -104.8214, "America/Denver"),
    "aurora, co":         ("Aurora, CO",         39.7294, -104.8319, "America/Denver"),
    "fort collins, co":   ("Fort Collins, CO",   40.5853, -105.0844, "America/Denver"),
    "boulder, co":        ("Boulder, CO",        40.0150, -105.2705, "America/Denver"),
    "longmont, co":       ("Longmont, CO",       40.1672, -105.1019, "America/Denver"),
    "pueblo, co":         ("Pueblo, CO",         38.2544, -104.6091, "America/Denver"),
    "greeley, co":        ("Greeley, CO",        40.4233, -104.7091, "America/Denver"),
    # Texas
    "houston, tx":        ("Houston, TX",        29.7604,  -95.3698, "America/Chicago"),
    "san antonio, tx":    ("San Antonio, TX",    29.4241,  -98.4936, "America/Chicago"),
    "dallas, tx":         ("Dallas, TX",         32.7767,  -96.7970, "America/Chicago"),
    "austin, tx":         ("Austin, TX",         30.2672,  -97.7431, "America/Chicago"),
    "fort worth, tx":     ("Fort Worth, TX",     32.7555,  -97.3308, "America/Chicago"),
    "el paso, tx":        ("El Paso, TX",        31.7619, -106.4850, "America/Denver"),
    "lubbock, tx":        ("Lubbock, TX",        33.5779, -101.8552, "America/Chicago"),
    # Midwest
    "chicago, il":        ("Chicago, IL",        41.8781,  -87.6298, "America/Chicago"),
    "aurora, il":         ("Aurora, IL",         41.7606,  -88.3201, "America/Chicago"),
    "rockford, il":       ("Rockford, IL",       42.2711,  -89.0940, "America/Chicago"),
    "springfield, il":    ("Springfield, IL",    39.7817,  -89.6501, "America/Chicago"),
    "detroit, mi":        ("Detroit, MI",        42.3314,  -83.0458, "America/Detroit"),
    "grand rapids, mi":   ("Grand Rapids, MI",   42.9634,  -85.6681, "America/Detroit"),
    "minneapolis, mn":    ("Minneapolis, MN",    44.9778,  -93.2650, "America/Chicago"),
    "st. paul, mn":       ("St. Paul, MN",       44.9537,  -93.0900, "America/Chicago"),
    "saint paul, mn":     ("St. Paul, MN",       44.9537,  -93.0900, "America/Chicago"),
    "rochester, mn":      ("Rochester, MN",      44.0234,  -92.4695, "America/Chicago"),
    "kansas city, mo":    ("Kansas City, MO",    39.0997,  -94.5786, "America/Chicago"),
    "st. louis, mo":      ("St. Louis, MO",      38.6270,  -90.1994, "America/Chicago"),
    "saint louis, mo":    ("St. Louis, MO",      38.6270,  -90.1994, "America/Chicago"),
    "springfield, mo":    ("Springfield, MO",    37.2090,  -93.2923, "America/Chicago"),
    "omaha, ne":          ("Omaha, NE",          41.2565,  -95.9345, "America/Chicago"),
    "lincoln, ne":        ("Lincoln, NE",        40.8136,  -96.7026, "America/Chicago"),
    "milwaukee, wi":      ("Milwaukee, WI",      43.0389,  -87.9065, "America/Chicago"),
    "madison, wi":        ("Madison, WI",        43.0731,  -89.4012, "America/Chicago"),
    "cleveland, oh":      ("Cleveland, OH",      41.4993,  -81.6944, "America/New_York"),
    "columbus, oh":       ("Columbus, OH",       39.9612,  -82.9988, "America/New_York"),
    "cincinnati, oh":     ("Cincinnati, OH",     39.1031,  -84.5120, "America/New_York"),
    "toledo, oh":         ("Toledo, OH",         41.6639,  -83.5552, "America/New_York"),
    "springfield, oh":    ("Springfield, OH",    39.9242,  -83.8088, "America/New_York"),
    "indianapolis, in":   ("Indianapolis, IN",   39.7684,  -86.1581, "America/Indiana/Indianapolis"),
    "fort wayne, in":     ("Fort Wayne, IN",     41.0793,  -85.1394, "America/Indiana/Indianapolis"),
    "des moines, ia":     ("Des Moines, IA",     41.5868,  -93.6250, "America/Chicago"),
    "wichita, ks":        ("Wichita, KS",        37.6872,  -97.3301, "America/Chicago"),
    "topeka, ks":         ("Topeka, KS",         39.0558,  -95.6890, "America/Chicago"),
    "sioux falls, sd":    ("Sioux Falls, SD",    43.5460,  -96.7313, "America/Chicago"),
    "fargo, nd":          ("Fargo, ND",          46.8772,  -96.7898, "America/Chicago"),
    # South
    "atlanta, ga":        ("Atlanta, GA",        33.7490,  -84.3880, "America/New_York"),
    "columbus, ga":       ("Columbus, GA",       32.4610,  -84.9877, "America/New_York"),
    "charlotte, nc":      ("Charlotte, NC",      35.2271,  -80.8431, "America/New_York"),
    "raleigh, nc":        ("Raleigh, NC",        35.7796,  -78.6382, "America/New_York"),
    "greensboro, nc":     ("Greensboro, NC",     36.0726,  -79.7920, "America/New_York"),
    "durham, nc":         ("Durham, NC",         35.9940,  -78.8986, "America/New_York"),
    "nashville, tn":      ("Nashville, TN",      36.1627,  -86.7816, "America/Chicago"),
    "memphis, tn":        ("Memphis, TN",        35.1495,  -90.0490, "America/Chicago"),
    "knoxville, tn":      ("Knoxville, TN",      35.9606,  -83.9207, "America/New_York"),
    "birmingham, al":     ("Birmingham, AL",     33.5186,  -86.8104, "America/Chicago"),
    "mobile, al":         ("Mobile, AL",         30.6954,  -88.0399, "America/Chicago"),
    "new orleans, la":    ("New Orleans, LA",    29.9511,  -90.0715, "America/Chicago"),
    "baton rouge, la":    ("Baton Rouge, LA",    30.4515,  -91.1871, "America/Chicago"),
    "jackson, ms":        ("Jackson, MS",        32.2988,  -90.1848, "America/Chicago"),
    "little rock, ar":    ("Little Rock, AR",    34.7465,  -92.2896, "America/Chicago"),
    "oklahoma city, ok":  ("Oklahoma City, OK",  35.4676,  -97.5164, "America/Chicago"),
    "tulsa, ok":          ("Tulsa, OK",          36.1540,  -95.9928, "America/Chicago"),
    "miami, fl":          ("Miami, FL",          25.7617,  -80.1918, "America/New_York"),
    "tampa, fl":          ("Tampa, FL",          27.9506,  -82.4572, "America/New_York"),
    "orlando, fl":        ("Orlando, FL",        28.5383,  -81.3792, "America/New_York"),
    "jacksonville, fl":   ("Jacksonville, FL",   30.3322,  -81.6557, "America/New_York"),
    "st. petersburg, fl": ("St. Petersburg, FL", 27.7676,  -82.6403, "America/New_York"),
    "richmond, va":       ("Richmond, VA",       37.5407,  -77.4360, "America/New_York"),
    "virginia beach, va": ("Virginia Beach, VA", 36.8529,  -75.9780, "America/New_York"),
    "norfolk, va":        ("Norfolk, VA",        36.8508,  -76.2859, "America/New_York"),
    "florence, sc":       ("Florence, SC",       34.1954,  -79.7626, "America/New_York"),
    "florence, al":       ("Florence, AL",       34.7998,  -87.6773, "America/Chicago"),
    "manchester, tn":     ("Manchester, TN",     35.4820,  -86.0886, "America/Chicago"),
    "miami, ok":          ("Miami, OK",          36.8742,  -94.8769, "America/Chicago"),
    # Northeast
    "philadelphia, pa":   ("Philadelphia, PA",   39.9526,  -75.1652, "America/New_York"),
    "pittsburgh, pa":     ("Pittsburgh, PA",     40.4406,  -79.9959, "America/New_York"),
    "boston, ma":         ("Boston, MA",         42.3601,  -71.0589, "America/New_York"),
    "worcester, ma":      ("Worcester, MA",      42.2626,  -71.8023, "America/New_York"),
    "springfield, ma":    ("Springfield, MA",    42.1015,  -72.5898, "America/New_York"),
    "providence, ri":     ("Providence, RI",     41.8240,  -71.4128, "America/New_York"),
    "bridgeport, ct":     ("Bridgeport, CT",     41.1792,  -73.1894, "America/New_York"),
    "hartford, ct":       ("Hartford, CT",       41.7658,  -72.6851, "America/New_York"),
    "new haven, ct":      ("New Haven, CT",      41.3083,  -72.9279, "America/New_York"),
    "newark, nj":         ("Newark, NJ",         40.7357,  -74.1724, "America/New_York"),
    "jersey city, nj":    ("Jersey City, NJ",    40.7178,  -74.0431, "America/New_York"),
    "manchester, nh":     ("Manchester, NH",     42.9956,  -71.4548, "America/New_York"),
    "burlington, vt":     ("Burlington, VT",     44.4759,  -73.2121, "America/New_York"),
    "richmond, ca":       ("Richmond, CA",       37.9358, -122.3478, "America/Los_Angeles"),
    # Mountain West
    "phoenix, az":        ("Phoenix, AZ",        33.4484, -112.0740, "America/Phoenix"),
    "tucson, az":         ("Tucson, AZ",         32.2226, -110.9747, "America/Phoenix"),
    "mesa, az":           ("Mesa, AZ",           33.4152, -111.8315, "America/Phoenix"),
    "chandler, az":       ("Chandler, AZ",       33.3062, -111.8413, "America/Phoenix"),
    "scottsdale, az":     ("Scottsdale, AZ",     33.4942, -111.9261, "America/Phoenix"),
    "las vegas, nv":      ("Las Vegas, NV",      36.1699, -115.1398, "America/Los_Angeles"),
    "henderson, nv":      ("Henderson, NV",      36.0395, -114.9817, "America/Los_Angeles"),
    "reno, nv":           ("Reno, NV",           39.5296, -119.8138, "America/Los_Angeles"),
    "salt lake city, ut": ("Salt Lake City, UT", 40.7608, -111.8910, "America/Denver"),
    "albuquerque, nm":    ("Albuquerque, NM",    35.0844, -106.6504, "America/Denver"),
    "billings, mt":       ("Billings, MT",       45.7833, -108.5007, "America/Denver"),
    "boise, id":          ("Boise, ID",          43.6150, -116.2023, "America/Boise"),
    "cheyenne, wy":       ("Cheyenne, WY",       41.1400, -104.8202, "America/Denver"),
    "casper, wy":         ("Casper, WY",         42.8501, -106.3252, "America/Denver"),
    # Alaska / Hawaii
    "anchorage, ak":      ("Anchorage, AK",      61.2181, -149.9003, "America/Anchorage"),
    "fairbanks, ak":      ("Fairbanks, AK",      64.8378, -147.7164, "America/Anchorage"),
    "honolulu, hi":       ("Honolulu, HI",       21.3069, -157.8583, "Pacific/Honolulu"),
    "hilo, hi":           ("Hilo, HI",           19.7297, -155.0900, "Pacific/Honolulu"),
}

STATE_ABBR: dict[str, str] = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}

ZIP_RE = re.compile(r"^\d{5}$")


def _build_city_only_index() -> dict[str, tuple]:
    """
    Map bare city name → TOP_CITIES entry for cities that:
      - appear exactly once in TOP_CITIES (unambiguous by count), AND
      - are not listed in AMBIGUOUS_CITIES

    Lets "Minneapolis" resolve to "Minneapolis, MN" without a state suffix
    or a Nominatim call, while leaving genuinely ambiguous cities (Portland,
    Aurora, …) to the existing AMBIGUOUS_CITIES path.
    """
    from collections import Counter
    counts: Counter = Counter()
    for key in TOP_CITIES:
        if "," in key:
            counts[key.split(",")[0].strip()] += 1

    index: dict[str, tuple] = {}
    for key, entry in TOP_CITIES.items():
        if "," not in key:
            continue
        city = key.split(",")[0].strip()
        if counts[city] == 1 and city not in AMBIGUOUS_CITIES:
            index[city] = entry
    return index


CITY_ONLY_INDEX: dict[str, tuple] = _build_city_only_index()


class LocationResolver:
    """
    Resolves a raw location string or lat/lon pair to ResolvedLocation objects.

    String input — returns a list (multiple only for ambiguous cities):
        resolver.resolve("Denver, CO")
        resolver.resolve("80501")
        resolver.resolve("Portland")   # → [Portland OR, Portland ME]

    Lat/lon input — returns a single ResolvedLocation:
        resolver.resolve_latlon(40.17, -105.10)
    """

    def resolve(self, raw: str) -> list[ResolvedLocation]:
        raw = raw.strip()
        if ZIP_RE.match(raw):
            return [self._resolve_zip(raw)]
        return self._resolve_city(raw)

    def resolve_latlon(self, lat: float, lon: float) -> ResolvedLocation:
        """
        Reverse-geocode a lat/lon pair to a ResolvedLocation.

        Resolution order:
          1. Nearest entry in TOP_CITIES within 50 km (offline, fast)
          2. Nominatim reverse geocoding (network, returns zip + city)
          3. Bare coordinate fallback
        """
        tz = self._tz_from_latlon(lat, lon)

        # --- 1. Nearest top city (offline) ---
        nearest = self._nearest_top_city(lat, lon, max_km=50)
        if nearest:
            return ResolvedLocation(
                lat=lat, lon=lon,
                display_name=nearest[0],
                timezone=tz or nearest[3],
            )

        # --- 2. Nominatim reverse geocoding ---
        try:
            from geopy.geocoders import Nominatim as GeoNom
            from geopy.exc import GeocoderTimedOut
            geolocator = GeoNom(user_agent="bluesky_weather_bot/1.0")
            loc = geolocator.reverse((lat, lon), timeout=5)
            if loc:
                addr = loc.raw.get("address", {})
                city  = (addr.get("city") or addr.get("town") or
                         addr.get("village") or addr.get("county", ""))
                state = addr.get("state_code") or addr.get("state", "")
                state = state[:2].upper() if state else ""
                zip_code = addr.get("postcode")
                display = f"{city}, {state}" if city and state else city or f"{lat:.4f}, {lon:.4f}"
                return ResolvedLocation(
                    lat=lat, lon=lon,
                    display_name=display,
                    timezone=tz,
                    zip_code=zip_code,
                )
        except Exception as e:
            logger.warning("Reverse geocode failed for (%s, %s): %s", lat, lon, e)

        # --- 3. Bare coordinate fallback ---
        return ResolvedLocation(
            lat=lat, lon=lon,
            display_name=f"{lat:.4f}, {lon:.4f}",
            timezone=tz or "UTC",
        )

    def _resolve_zip(self, zip_code: str) -> ResolvedLocation:
        try:
            import pgeocode
            nomi = pgeocode.Nominatim("us")
            result = nomi.query_postal_code(zip_code)
            if result is None or result.latitude != result.latitude:  # NaN check
                raise ValueError(f"Zip code {zip_code!r} not found.")
            lat = float(result.latitude)
            lon = float(result.longitude)
            sc  = str(result.state_code).upper()
            city = str(result.place_name)
            display = f"{city}, {sc}" if sc else city
            tz = self._tz_from_latlon(lat, lon, sc)
            return ResolvedLocation(
                lat=lat, lon=lon, display_name=display,
                timezone=tz, zip_code=zip_code,
            )
        except ImportError:
            raise RuntimeError("pgeocode required for zip lookup: pip install pgeocode")

    def _resolve_city(self, raw: str) -> list[ResolvedLocation]:
        normalized = self._normalize(raw)

        # 1. Direct hit: "denver, co" → TOP_CITIES["denver, co"]
        if normalized in TOP_CITIES:
            e = TOP_CITIES[normalized]
            return [ResolvedLocation(lat=e[1], lon=e[2], display_name=e[0], timezone=e[3])]

        city_only = normalized.split(",")[0].strip() if "," in normalized else normalized

        # 2. Unambiguous city name without state: "minneapolis" → CITY_ONLY_INDEX
        if city_only in CITY_ONLY_INDEX and "," not in normalized:
            e = CITY_ONLY_INDEX[city_only]
            return [ResolvedLocation(lat=e[1], lon=e[2], display_name=e[0], timezone=e[3])]

        # 3. Ambiguous?
        if city_only in AMBIGUOUS_CITIES and "," not in normalized:
            candidates = [
                ResolvedLocation(lat=c[1], lon=c[2], display_name=c[0],
                                 timezone=c[3], input_was_ambiguous=True)
                for c in AMBIGUOUS_CITIES[city_only]
            ]
            for c in candidates:
                c.candidates = candidates
            return candidates

        # Nominatim fallback
        return [self._resolve_nominatim(raw)]

    def _resolve_nominatim(self, raw: str) -> ResolvedLocation:
        try:
            from geopy.geocoders import Nominatim as GeoNom
            from geopy.exc import GeocoderTimedOut
        except ImportError:
            raise RuntimeError("geopy required for fallback geocoding: pip install geopy")

        geolocator = GeoNom(user_agent="bluesky_weather_bot/1.0")
        query = raw if "us" in raw.lower() else f"{raw}, USA"
        try:
            loc = geolocator.geocode(query, timeout=5)
        except GeocoderTimedOut:
            raise ValueError(f"Geocoding timed out for {raw!r}")
        if loc is None:
            raise ValueError(f"Could not geocode: {raw!r}")
        lat, lon = loc.latitude, loc.longitude
        parts = loc.address.split(",")
        display = ", ".join(p.strip() for p in parts[:2])
        return ResolvedLocation(lat=lat, lon=lon, display_name=display,
                                timezone=self._tz_from_lon(lon))

    @staticmethod
    def _normalize(raw: str) -> str:
        """Normalize 'Denver CO', 'Denver, Colorado', 'Denver, CO' → 'denver, co'."""
        s = raw.strip().lower()
        # Full state name suffix: "Denver, Colorado" → "denver, co"
        for full, abbr in STATE_ABBR.items():
            if s.endswith(f", {full}") or s.endswith(f" {full}"):
                city = re.sub(rf",?\s*{re.escape(full)}$", "", s).strip()
                return f"{city}, {abbr}"
        # Already "city, st" — handle before the no-comma regex to avoid "city,, st"
        if "," in s:
            parts = s.split(",", 1)
            return f"{parts[0].strip()}, {parts[1].strip()}"
        # "City ST" pattern (two-letter code, no comma)
        m = re.match(r"^(.+?)\s+([a-z]{2})$", s)
        if m:
            city_part, state_part = m.group(1).strip(), m.group(2)
            if state_part in set(STATE_ABBR.values()):
                return f"{city_part}, {state_part}"
        if "," in s:
            parts = s.split(",", 1)
            return f"{parts[0].strip()}, {parts[1].strip()}"
        return s

    @staticmethod
    def _tz_from_latlon(lat: float, lon: float, state_code: str = "") -> str:
        """Return IANA timezone string for (lat, lon), with longitude-band fallback."""
        try:
            from timezonefinder import TimezoneFinder
            tz = TimezoneFinder().timezone_at(lat=lat, lng=lon)
            if tz:
                return tz
        except ImportError:
            pass
        sc = state_code.upper()
        if sc == "AK": return "America/Anchorage"
        if sc == "HI": return "Pacific/Honolulu"
        if sc == "AZ": return "America/Phoenix"
        if lon < -115: return "America/Los_Angeles"
        if lon < -104: return "America/Denver"
        if lon < -87:  return "America/Chicago"
        return "America/New_York"

    # Keep the old name as an alias so existing call sites don't break
    @staticmethod
    def _tz_from_lon(lon: float, state_code: str = "") -> str:
        return LocationResolver._tz_from_latlon(39.5, lon, state_code)

    @staticmethod
    def _nearest_top_city(
        lat: float, lon: float, max_km: float = 50.0
    ) -> Optional[tuple]:
        """Return the TOP_CITIES entry (display, lat, lon, tz) nearest to (lat, lon),
        or None if the nearest is farther than max_km."""
        best_entry = None
        best_km    = float("inf")
        for entry in TOP_CITIES.values():
            km = _haversine_km(lat, lon, entry[1], entry[2])
            if km < best_km:
                best_km    = km
                best_entry = entry
        return best_entry if best_km <= max_km else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))
