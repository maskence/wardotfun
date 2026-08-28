"""
Populate wardotfun/backend/data/city_market_map.json from Polymarket Gamma API.

Usage: python utils/manage_city_market_map.py [--mapping-file PATH]
"""
import argparse
import fcntl
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

DEFAULT_MAPPING_FILE = (
    Path(__file__).resolve().parents[1] / "backend" / "data" / "city_market_map.json"
)
SETTLEMENTS_LAYER_URL = (
    "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
    "Ukrainian_Settlements_Updated_view/FeatureServer/0/query"
)

# Markets whose extracted city name matches one of these (case-insensitive) are silently skipped.
CITY_NAME_BLOCKLIST = [
    "donetsk oblast",
]

GAMMA_SEARCH_URLS = [
    "https://gamma-api.polymarket.com/public-search?q=russia%20enter"
    "&events_status=active&limit_per_type=200&page=1&keep_closed_markets=0",
    "https://gamma-api.polymarket.com/public-search?q=russia%20capture"
    "&events_status=active&limit_per_type=200&page=1&keep_closed_markets=0",
]
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
SCHEMA_VERSION = 3


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Rebuild city-market map for wardotfun.")
    parser.add_argument("--mapping-file", default=None, type=Path)
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Skip markets that require city or target input instead of prompting.",
    )
    return parser.parse_args()


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch_json(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


# ── Gamma API helpers ─────────────────────────────────────────────────────────

def iter_events(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return events
    return []


def iter_markets(event):
    markets = event.get("markets") or []
    return markets if isinstance(markets, list) else []


def decode_list(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolved_outcome(market):
    outcomes = decode_list(market.get("outcomes"))
    prices = [parse_float(value) for value in decode_list(market.get("outcomePrices"))]
    settled = [(index, price) for index, price in enumerate(prices)
               if price is not None and price >= 0.99]
    if not settled:
        return None
    index = max(settled, key=lambda item: item[1])[0]
    return str(outcomes[index]) if index < len(outcomes) else None


def remote_market_status(market):
    if not market.get("closed"):
        return {"status": "active"}
    closed_at = (market.get("closedTime") or market.get("resolutionTime")
                 or market.get("updatedAt") or market.get("endDate"))
    outcome = resolved_outcome(market)
    if not outcome:
        return {"status": "closed", "closedAt": closed_at}
    resolved_at = (market.get("resolutionTime") or market.get("closedTime")
                   or market.get("updatedAt") or market.get("endDate"))
    return {
        "status": "resolved",
        "outcome": outcome,
        "resolvedAt": resolved_at,
        "closedAt": closed_at,
    }


def apply_remote_status(entry, remote):
    status = remote_market_status(remote)
    entry.pop("active", None)
    for key in ("outcome", "resolvedAt", "closedAt"):
        entry.pop(key, None)
    entry.update({key: value for key, value in status.items() if value is not None})


def migrate_market_status(entry):
    if entry.get("status") in {"active", "resolved", "closed"}:
        entry.pop("active", None)
        return
    entry["status"] = "active" if entry.get("active") is not False else "closed"
    entry.pop("active", None)


def fetch_gamma_markets(market_ids):
    markets = {}
    for closed in (False, True):
        missing = [market_id for market_id in market_ids if market_id not in markets]
        for offset in range(0, len(missing), 100):
            chunk = missing[offset:offset + 100]
            params = [("id", market_id) for market_id in chunk]
            params.extend((("limit", str(len(chunk))), ("closed", str(closed).lower())))
            response = fetch_json(f"{GAMMA_MARKETS_URL}?{urlencode(params)}")
            for market in response if isinstance(response, list) else []:
                if market.get("id") is not None:
                    markets[str(market["id"])] = market
    return markets


def fetch_gamma_events(event_slugs):
    events = {}
    for offset in range(0, len(event_slugs), 20):
        chunk = event_slugs[offset:offset + 20]
        params = [("slug", slug) for slug in chunk]
        params.append(("limit", str(len(chunk))))
        response = fetch_json(f"{GAMMA_EVENTS_URL}?{urlencode(params)}")
        for event in response if isinstance(response, list) else []:
            if event.get("slug"):
                events[event["slug"]] = event
    return events


def extract_deadline(market, event):
    for key in ("endDate", "closeTime", "closedTime", "closeDate",
                "closeDateTime", "endTime", "resolutionTime"):
        v = market.get(key) or event.get(key)
        if v:
            return v
    return None


def market_display_name(market, event):
    return (market.get("question") or market.get("title") or event.get("title")
            or market.get("slug") or market.get("id") or "unknown")


def build_market_entry(market, event):
    """Build a market entry with the wardotfun display schema."""
    market_id = market.get("id")
    condition_id = market.get("conditionId")
    if not market_id or not condition_id:
        return None

    event_slug = event.get("slug")
    if not event_slug:
        return None

    entry = {
        "id": str(market_id),
        "conditionId": condition_id,
        "title": market.get("question") or market.get("title") or event.get("title"),
        "slug": market.get("slug"),
        "eventSlug": event_slug,
        "deadline": extract_deadline(market, event),
    }
    entry.update(remote_market_status(market))
    return {k: v for k, v in entry.items() if v is not None}


def resolve_capture_target(title, text_candidates, interactive=True):
    """Try to extract target coordinates for a capture market. Prompt user if not found."""
    coords = find_coordinates(text_candidates)
    if not coords:
        coords = find_google_maps_coordinates(text_candidates)
    if coords:
        lat, lon = coords
        return {"lat": lat, "lon": lon}
    if not interactive:
        return None
    while True:
        sel = input(
            f"  No target coords found for capture market:\n"
            f"  '{title}'\n"
            f"  Enter 'lat lon' (e.g. 48.5472 37.3742) or 's' to skip: "
        ).strip()
        if sel.lower() == "s":
            return None
        parts = sel.split()
        if len(parts) == 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
                if abs(lat) <= 90 and abs(lon) <= 180:
                    return {"lat": lat, "lon": lon}
            except ValueError:
                pass
        print("  Invalid input. Enter decimal coordinates or 's' to skip.")


# ── Market type / city inference ──────────────────────────────────────────────

def slug_to_title(slug):
    return slug.replace("-", " ").replace("_", " ").strip() if slug else None


def infer_market_type(text):
    if not text:
        return None
    t = text.lower()
    if "capture all" in t:
        return "capture_all"
    if "capture" in t:
        return "capture"
    if "enter" in t:
        return "enter"
    return None


def infer_market_type_from_texts(texts):
    for text in texts:
        mt = infer_market_type(text or "")
        if mt:
            return mt
    return None


def extract_city_name(text):
    if not text:
        return None
    for pattern in (
        r"capture all(?: of)?\s+(.*?)\s+by",
        r"capture\s+(.*?)\s+by",
        r"enter\s+(.*?)\s+by",
    ):
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_city_from_description(text):
    if not text:
        return None
    for pattern in (
        r"territory of\s+([^,]+)",
        r"in the city of\s+([^,]+)",
        r"in\s+([^,]+)\s+oblast",
    ):
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_city_name_from_texts(texts):
    for text in texts:
        c = extract_city_name(text or "")
        if c:
            return c
    for text in texts:
        c = extract_city_from_description(text or "")
        if c:
            return c
    return None


# ── Coordinate helpers ────────────────────────────────────────────────────────

def extract_coordinates(text):
    if not text:
        return None
    for pattern in (
        r"(?P<lat>-?\d+\.\d+)\s*[°º]?\s*[Nn].*?(?P<lon>-?\d+\.\d+)\s*[°º]?\s*[Ee]",
        r"lat(?:itude)?\s*[:=]\s*(?P<lat>-?\d+\.\d+).*?lon(?:gitude)?\s*[:=]\s*(?P<lon>-?\d+\.\d+)",
        r"!3d(?P<lat>-?\d+\.\d+)!4d(?P<lon>-?\d+\.\d+)",
        r"@(?P<lat>-?\d+\.\d+),(?P<lon>-?\d+\.\d+)",
    ):
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group("lat")), float(m.group("lon"))
    m = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if abs(lat) > 90 and abs(lon) <= 90:
            lat, lon = lon, lat
        if abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    return None


def find_coordinates(texts):
    for text in texts:
        c = extract_coordinates(text or "")
        if c:
            return c
    return None


def google_maps_urls(texts):
    urls = []
    for text in texts:
        urls.extend(re.findall(
            r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[^/\s]+/maps)/[^\s<>)]+",
            text or "", flags=re.IGNORECASE,
        ))
    return urls


def find_google_maps_coordinates(texts):
    """Resolve trusted Google Maps links and extract their destination coordinates."""
    for url in google_maps_urls(texts):
        host = (urlparse(url).hostname or "").lower()
        if not (host == "maps.app.goo.gl" or host == "goo.gl" or host.startswith("google.")
                or host.startswith("www.google.")):
            continue
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as response:
                destination = response.geturl()
        except OSError:
            continue
        coords = extract_coordinates(destination)
        if coords:
            return coords
    return None


def capture_target_label(event, market, city_name=None):
    """Return the objective name shared by every deadline in a capture event."""
    title = event.get("title") or market.get("question") or market.get("title") or ""
    specific = re.search(r"capture\s+(?:the\s+)?(.+?)\s+in\s+.+?\s+by", title, re.IGNORECASE)
    if specific:
        return specific.group(1).strip()

    description = market.get("description") or event.get("description") or ""
    described = re.search(r"captures?\s+the\s+(.+?)\s+(?:located|at\s+-?\d)", description,
                          re.IGNORECASE)
    if described:
        label = described.group(1).strip()
        if city_name:
            label = re.sub(rf"^{re.escape(city_name)}\s+", "", label, flags=re.IGNORECASE)
        return label[:1].upper() + label[1:]

    stem = re.sub(r"^Will Russia capture\s+(?:the\s+)?", "", title, flags=re.IGNORECASE)
    stem = re.sub(r"\s+by.*$", "", stem, flags=re.IGNORECASE).strip(" ?")
    return stem or "Capture target"


# ── ArcGIS settlement lookup ──────────────────────────────────────────────────

def query_settlement(where=None, geometry=None, geometry_type=None, in_sr=None):
    params = []
    if where:
        params.append(f"where={quote(where)}")
    if geometry:
        params.append(f"geometry={quote(geometry)}")
    if geometry_type:
        params.append(f"geometryType={geometry_type}")
    if in_sr:
        params.append(f"inSR={in_sr}")
    params += [
        "outSR=3857",
        "spatialRel=esriSpatialRelIntersects",
        "outFields=ADM4_EN,ADM4_UA,ADM3_EN,ADM3_UA,ADM2_EN,ADM2_UA,ADM1_EN,ADM1_UA,OBJECTID,GlobalID",
        "returnGeometry=true",
        "cacheHint=true",
        "f=json",
    ]
    data = fetch_json(f"{SETTLEMENTS_LAYER_URL}?" + "&".join(params))
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        code = err.get("code")
        msg = err.get("message") or "ArcGIS query failed."
        details = "; ".join(err.get("details") or [])
        if details:
            msg = f"{msg} {details}"
        if code == 429:
            raise SystemExit(f"ArcGIS rate limit (429). {msg}")
        raise SystemExit(f"ArcGIS error ({code}): {msg}")
    return data.get("features", [])


def first_coordinate(geometry):
    rings = (geometry or {}).get("rings")
    return rings[0][0] if rings and rings[0] else None


def project_to_lonlat(point):
    if not point:
        return None
    x, y = point
    R = 6378137.0
    return [(x / R) * (180 / math.pi),
            (2 * math.atan(math.exp(y / R)) - math.pi / 2) * (180 / math.pi)]


def normalize_city_name(name):
    if not name:
        return None
    s = name.replace("\u2019", "'")
    s = re.sub(r"\s*\(.*?\)", "", s)
    s = s.split(",", 1)[0].strip()
    s = re.sub(r"\b(once\s+)?again\b$", "", s, flags=re.IGNORECASE).strip()
    low = s.lower()
    for prefix in ("the city of ", "city of ", "the town of ", "town of ",
                   "the village of ", "village of "):
        if low.startswith(prefix):
            s = s[len(prefix):]
            break
    return re.sub(r"\s+", " ", s).strip()


def build_name_variants(city_name):
    variants = []
    for name in (city_name, normalize_city_name(city_name)):
        if name and name not in variants:
            variants.append(name)
    expanded = []
    for name in variants:
        spaced = name.replace("-", " ")
        if spaced and spaced not in variants and spaced not in expanded:
            expanded.append(spaced)
    return variants + expanded


def query_city_by_name(city_name):
    for variant in build_name_variants(city_name):
        safe = variant.replace("'", "''")
        for field in ("ADM4_EN", "ADM4_UA"):
            features = query_settlement(where=f"UPPER({field})='{safe.upper()}'")
            if features:
                return features
    for variant in build_name_variants(city_name):
        safe = variant.replace("'", "''")
        for field in ("ADM4_EN", "ADM4_UA"):
            features = query_settlement(where=f"UPPER({field}) LIKE '%{safe.upper()}%'")
            if features:
                return features
    return []


def choose_feature(features, label, interactive=True):
    if len(features) == 1:
        return features[0]
    if not interactive:
        print(f"  Skip '{label}': {len(features)} settlement matches require selection")
        return None
    print(f"Found {len(features)} matches for '{label}':")
    for i, f in enumerate(features):
        attrs = f.get("attributes", {})
        lonlat = project_to_lonlat(first_coordinate(f.get("geometry")))
        coord_str = f"{lonlat[0]:.6f}, {lonlat[1]:.6f}" if lonlat else "unknown"
        adm4 = attrs.get("ADM4_EN") or "?"
        adm3 = attrs.get("ADM3_EN") or ""
        adm2 = attrs.get("ADM2_EN") or ""
        adm1 = attrs.get("ADM1_EN") or "?"
        hierarchy = " / ".join(filter(None, [adm4, adm3, adm2, adm1]))
        print(f"  [{i}] {hierarchy}  ({coord_str})")
    while True:
        sel = input("Select index (or 's' to skip): ").strip().lower()
        if sel == "s":
            return None
        try:
            idx = int(sel)
        except ValueError:
            print("Enter a number or 's'.")
            continue
        if 0 <= idx < len(features):
            return features[idx]
        print("Index out of range.")


def resolve_city_feature(city_name, text_candidates, interactive=True):
    coords = find_coordinates(text_candidates)
    if coords:
        lat, lon = coords
        features = query_settlement(
            geometry=f"{lon},{lat}", geometry_type="esriGeometryPoint", in_sr=4326)
        if features:
            selected = choose_feature(features, f"coords {lat:.5f},{lon:.5f}", interactive)
            if selected:
                return selected
        else:
            print(f"No settlement at {lat},{lon}; trying name lookup.")
    features = query_city_by_name(city_name)
    if not features and not interactive:
        return None
    while not features:
        fallback = input(
            f"No settlement found for '{city_name}'. Name or 's' to skip: ").strip()
        if fallback.lower() == "s":
            return None
        if fallback:
            city_name = fallback
            features = query_city_by_name(city_name)
    return choose_feature(features, city_name, interactive)


def build_city_payload(feature):
    attrs = feature.get("attributes", {})
    return {
        "name_en":   attrs.get("ADM4_EN"),
        "name_ua":   attrs.get("ADM4_UA"),
        "adm3_en":   attrs.get("ADM3_EN"),
        "adm3_ua":   attrs.get("ADM3_UA"),
        "adm2_en":   attrs.get("ADM2_EN"),
        "adm2_ua":   attrs.get("ADM2_UA"),
        "adm1_en":   attrs.get("ADM1_EN"),
        "adm1_ua":   attrs.get("ADM1_UA"),
        "object_id": attrs.get("OBJECTID"),
        "global_id": attrs.get("GlobalID"),
    }


# ── Mapping I/O ───────────────────────────────────────────────────────────────

def load_mapping(path: Path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "cities": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "cities": {}}


def save_mapping(path: Path, mapping):
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping["generated_at"] = datetime.now(timezone.utc).isoformat()
    mapping["schema_version"] = SCHEMA_VERSION
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ── Dedup helpers ─────────────────────────────────────────────────────────────

def collect_existing_keys(mapping):
    keys = set()
    for city_entry in mapping.get("cities", {}).values():
        for market_list in (city_entry.get("markets") or {}).values():
            for m in market_list:
                if m.get("id"):
                    keys.add(f"id:{m['id']}")
                if m.get("slug"):
                    keys.add(f"slug:{m['slug']}")
    return keys


def market_keys(market):
    keys = []
    if market.get("id"):
        keys.append(f"id:{market['id']}")
    if market.get("slug"):
        keys.append(f"slug:{market['slug']}")
    return keys


def is_duplicate(existing_keys, market_entry):
    return any(k in existing_keys for k in market_keys(market_entry))


def iter_stored_markets(mapping):
    for city_id, city_entry in mapping.get("cities", {}).items():
        for market_type, markets in (city_entry.get("markets") or {}).items():
            for market in markets:
                yield city_id, city_entry, market_type, market


def find_stored_market(mapping, candidate):
    candidate_keys = set(market_keys(candidate))
    for _, _, _, market in iter_stored_markets(mapping):
        if candidate_keys.intersection(market_keys(market)):
            return market
    return None


def update_market_metadata(stored, remote_entry):
    for key in ("conditionId", "title", "slug", "eventSlug", "deadline"):
        if remote_entry.get(key) is not None:
            stored[key] = remote_entry[key]
    stored["status"] = remote_entry["status"]
    for key in ("outcome", "resolvedAt", "closedAt"):
        if key in remote_entry:
            stored[key] = remote_entry[key]
        else:
            stored.pop(key, None)
    stored.pop("active", None)


def normalized_question_stem(title):
    normalized = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    return re.sub(r"\s+by\s+.+$", "", normalized).strip()


def existing_event_contexts(mapping, event_slug):
    contexts = []
    for city_id, city_entry, market_type, market in iter_stored_markets(mapping):
        if market.get("eventSlug") != event_slug:
            continue
        contexts.append({
            "city_id": city_id,
            "city_entry": city_entry,
            "market_type": market_type,
            "stem": normalized_question_stem(market.get("title")),
        })
    return contexts


def find_existing_city(mapping, city_name):
    wanted = normalize_city_name(city_name)
    wanted = wanted.lower() if wanted else None
    if not wanted:
        return None
    for city_id, city_entry in mapping.get("cities", {}).items():
        name = normalize_city_name((city_entry.get("city") or {}).get("name_en"))
        if name and name.lower() == wanted:
            return city_id, city_entry
    return None


def existing_event_target(city_entry, market_type, event_slug):
    for market in (city_entry.get("markets") or {}).get(market_type, []):
        if market.get("eventSlug") == event_slug and market.get("target"):
            return market["target"]
    return None


def resolve_backfill_city(mapping, event, market, interactive=True):
    event_slug = event.get("slug")
    contexts = existing_event_contexts(mapping, event_slug)
    stem = normalized_question_stem(market_display_name(market, event))
    stem_matches = [context for context in contexts if context["stem"] == stem]
    if stem_matches:
        context = stem_matches[0]
        return context["city_id"], context["city_entry"]

    text_candidates = [
        market.get("question"), market.get("title"), slug_to_title(market.get("slug")),
        event.get("title"), slug_to_title(event_slug), event.get("ticker"),
        market.get("rules"), market.get("description"),
        event.get("rules"), event.get("description"),
    ]
    city_name = extract_city_name_from_texts(text_candidates)
    existing = find_existing_city(mapping, city_name)
    if existing:
        return existing

    unique_cities = {context["city_id"]: context["city_entry"] for context in contexts}
    if len(unique_cities) == 1:
        return next(iter(unique_cities.items()))
    if not city_name or normalize_city_name(city_name).lower() in CITY_NAME_BLOCKLIST:
        return None

    city_feature = resolve_city_feature(city_name, text_candidates, interactive)
    if not city_feature:
        return None
    city_payload = build_city_payload(city_feature)
    city_id = city_payload.get("global_id")
    if not city_id:
        return None
    city_entry = mapping.setdefault("cities", {}).setdefault(city_id, {
        "city": city_payload,
        "geometry": city_feature.get("geometry"),
        "markets": {"enter": [], "capture": [], "capture_all": []},
    })
    return city_id, city_entry


def backfill_known_events(mapping, existing_keys, interactive=True):
    event_slugs = sorted({
        market.get("eventSlug") for _, _, _, market in iter_stored_markets(mapping)
        if market.get("eventSlug")
    })
    events = fetch_gamma_events(event_slugs)
    added = unresolved = 0
    for event_slug in event_slugs:
        event = events.get(event_slug)
        if not event:
            continue
        for remote in iter_markets(event):
            if not remote.get("closed"):
                continue
            entry = build_market_entry(remote, event)
            if not entry or is_duplicate(existing_keys, entry):
                continue
            destination = resolve_backfill_city(mapping, event, remote, interactive)
            market_type = infer_market_type_from_texts([
                remote.get("question"), remote.get("title"), event.get("title"),
                slug_to_title(remote.get("slug")), slug_to_title(event_slug),
            ])
            if not destination or not market_type:
                unresolved += 1
                print(f"  Skip historical '{market_display_name(remote, event)}': unresolved city/type")
                continue
            _, city_entry = destination
            city_entry.setdefault("markets", {"enter": [], "capture": [], "capture_all": []})
            city_entry["markets"].setdefault(market_type, []).append(entry)
            for key in market_keys(entry):
                existing_keys.add(key)
            added += 1
            city_name = (city_entry.get("city") or {}).get("name_en") or "unknown"
            print(f"  Backfilled [{market_type}] '{entry.get('title', '?')}' → {city_name}")
    return added, unresolved


# ── Main processing ───────────────────────────────────────────────────────────

def add_market(mapping, existing_keys, entry, city_payload, geometry, market_type):
    if is_duplicate(existing_keys, entry):
        return False
    city_id = city_payload.get("global_id")
    if not city_id:
        raise SystemExit("City payload missing global_id.")
    cities = mapping.setdefault("cities", {})
    city_entry = cities.setdefault(city_id, {
        "city": city_payload,
        "geometry": geometry,
        "markets": {"enter": [], "capture": [], "capture_all": []},
    })
    city_entry.setdefault("city", city_payload)
    if geometry and not city_entry.get("geometry"):
        city_entry["geometry"] = geometry
    city_entry.setdefault("markets", {"enter": [], "capture": [], "capture_all": []})
    city_entry["markets"].setdefault(market_type, []).append(entry)
    for k in market_keys(entry):
        existing_keys.add(k)
    city_name = city_payload.get("name_en") or "unknown"
    print(f"  Added [{market_type}] '{entry.get('title', '?')}' → {city_name}")
    return True


def process_markets(mapping_file: Path, interactive=True):
    print("Fetching markets from Gamma API...")
    payloads = []
    for url in GAMMA_SEARCH_URLS:
        try:
            payloads.append(fetch_json(url))
        except OSError as exc:
            raise SystemExit(f"Gamma fetch failed: {exc}")

    mapping = load_mapping(mapping_file)
    for _, _, _, market in iter_stored_markets(mapping):
        migrate_market_status(market)
    existing_keys = collect_existing_keys(mapping)
    city_cache = {}
    added = updated = 0
    unresolved_by_type: dict[str, int] = {}

    for payload in payloads:
        for event in iter_events(payload):
            for market in iter_markets(event):
                title = market_display_name(market, event)

                if market.get("closed"):
                    continue

                text_candidates = [
                    market.get("question"),
                    market.get("title"),
                    slug_to_title(market.get("slug")),
                    event.get("title"),
                    slug_to_title(event.get("slug")),
                    event.get("ticker"),
                ]

                market_type = infer_market_type_from_texts(text_candidates)
                if not market_type:
                    unresolved_by_type["unknown_type"] = unresolved_by_type.get("unknown_type", 0) + 1
                    print(f"  Skip '{title}': unknown type")
                    continue

                entry = build_market_entry(market, event)
                if not entry:
                    print(f"  Skip '{title}': missing id/conditionId/eventSlug")
                    continue

                stored = find_stored_market(mapping, entry)
                if stored:
                    update_market_metadata(stored, entry)
                    if (market_type == "capture" and stored.get("target")
                            and not stored["target"].get("label")):
                        stored["target"]["label"] = capture_target_label(event, market)
                    updated += 1
                    continue

                city_name = extract_city_name_from_texts(text_candidates)
                if not city_name:
                    city_name = extract_city_name_from_texts([
                        market.get("rules"), market.get("description"),
                        event.get("rules"), event.get("description"),
                    ])
                if not city_name:
                    unresolved_by_type[market_type] = unresolved_by_type.get(market_type, 0) + 1
                    print(f"  Skip [{market_type}] '{title}': could not extract city name")
                    continue

                if normalize_city_name(city_name).lower() in CITY_NAME_BLOCKLIST:
                    continue

                rules_texts = [
                    market.get("rules"), market.get("description"),
                    event.get("rules"), event.get("description"),
                ]
                cache_key = normalize_city_name(city_name) or city_name
                existing_city = find_existing_city(mapping, city_name)
                if existing_city:
                    _, existing_city_entry = existing_city
                    city_payload = existing_city_entry.get("city") or {}
                    geometry = existing_city_entry.get("geometry")
                else:
                    if cache_key in city_cache:
                        city_feature = city_cache[cache_key]
                    else:
                        try:
                            city_feature = resolve_city_feature(city_name, rules_texts, interactive)
                        except SystemExit as exc:
                            unresolved_by_type[market_type] = unresolved_by_type.get(market_type, 0) + 1
                            print(f"  Skip '{title}': {exc}")
                            continue
                        if city_feature is None:
                            unresolved_by_type[market_type] = unresolved_by_type.get(market_type, 0) + 1
                            print(f"  Skip [{market_type}] '{title}': user skipped city")
                            continue
                        city_cache[cache_key] = city_feature
                    city_payload = build_city_payload(city_feature)
                    geometry = city_feature.get("geometry")

                if market_type == "capture":
                    target_texts = text_candidates + [
                        market.get("rules"), market.get("description"),
                        event.get("rules"), event.get("description"),
                    ]
                    target = (existing_event_target(existing_city_entry, market_type, entry.get("eventSlug"))
                              if existing_city else None)
                    target = target or resolve_capture_target(
                        entry.get("title", "?"), target_texts, interactive)
                    if target:
                        target = dict(target)
                        target["label"] = capture_target_label(event, market, city_name)
                        entry["target"] = target
                    else:
                        unresolved_by_type["capture_no_target"] = (
                            unresolved_by_type.get("capture_no_target", 0) + 1
                        )
                        print(f"  Skip [capture] '{entry.get('title', '?')}': no target")
                        continue

                if add_market(mapping, existing_keys, entry, city_payload, geometry, market_type):
                    added += 1

    stored_by_id = {
        str(market["id"]): market for _, _, _, market in iter_stored_markets(mapping)
        if market.get("id")
    }
    print(f"Reconciling {len(stored_by_id)} stored markets by ID...")
    try:
        remote_markets = fetch_gamma_markets(list(stored_by_id))
    except OSError as exc:
        raise SystemExit(f"Gamma status reconciliation failed; mapping left unchanged: {exc}")
    reconciled = 0
    for market_id, remote in remote_markets.items():
        apply_remote_status(stored_by_id[market_id], remote)
        reconciled += 1

    print("Backfilling closed markets from known event families...")
    try:
        backfilled, historical_unresolved = backfill_known_events(
            mapping, existing_keys, interactive)
    except OSError as exc:
        raise SystemExit(f"Gamma history backfill failed; mapping left unchanged: {exc}")

    save_mapping(mapping_file, mapping)
    total_unresolved = sum(unresolved_by_type.values())
    status_counts = {"active": 0, "resolved": 0, "closed": 0}
    for _, _, _, market in iter_stored_markets(mapping):
        status_counts[market.get("status", "closed")] += 1
    print(f"\nDone: +{added} active added, {updated} active updated, "
          f"{reconciled} reconciled, +{backfilled} historical, "
          f"{total_unresolved + historical_unresolved} unresolved.")
    print("  Stored statuses: " + ", ".join(
        f"{count} {status}" for status, count in status_counts.items()))
    for key, label in (
        ("enter",             "enter markets without city"),
        ("capture",           "capture markets without city"),
        ("capture_all",       "capture_all markets without city"),
        ("capture_no_target", "capture markets without target"),
        ("unknown_type",      "markets with unknown type"),
    ):
        n = unresolved_by_type.get(key, 0)
        if n:
            print(f"  {n} {label}")
    print(f"Output: {mapping_file}")


def main():
    args = parse_args()
    mapping_file = args.mapping_file or DEFAULT_MAPPING_FILE
    lock_path = mapping_file.with_suffix(mapping_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        process_markets(mapping_file, interactive=not args.non_interactive)


if __name__ == "__main__":
    main()
