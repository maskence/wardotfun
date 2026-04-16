"""
Populate wardotfun/backend/data/city_market_map.json from Polymarket Gamma API.

Usage: python utils/manage_city_market_map.py [--mapping-file PATH]
"""
import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
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


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Rebuild city-market map for wardotfun.")
    parser.add_argument("--mapping-file", default=None, type=Path)
    return parser.parse_args()


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch_json(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req) as resp:
        return json.load(resp)


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
        "active": not bool(market.get("closed")),
    }
    return {k: v for k, v in entry.items() if v is not None}


def resolve_capture_target(title, text_candidates):
    """Try to extract target coordinates for a capture market. Prompt user if not found."""
    coords = find_coordinates(text_candidates)
    if coords:
        lat, lon = coords
        return {"lat": lat, "lon": lon}
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


def choose_feature(features, label):
    if len(features) == 1:
        return features[0]
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


def resolve_city_feature(city_name, text_candidates):
    coords = find_coordinates(text_candidates)
    if coords:
        lat, lon = coords
        features = query_settlement(
            geometry=f"{lon},{lat}", geometry_type="esriGeometryPoint", in_sr=4326)
        if features:
            selected = choose_feature(features, f"coords {lat:.5f},{lon:.5f}")
            if selected:
                return selected
        else:
            print(f"No settlement at {lat},{lon}; trying name lookup.")
    features = query_city_by_name(city_name)
    while not features:
        fallback = input(
            f"No settlement found for '{city_name}'. Name or 's' to skip: ").strip()
        if fallback.lower() == "s":
            return None
        if fallback:
            city_name = fallback
            features = query_city_by_name(city_name)
    return choose_feature(features, city_name)


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
        return {"schema_version": 2, "generated_at": "", "cities": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 2, "generated_at": "", "cities": {}}


def save_mapping(path: Path, mapping):
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping["generated_at"] = datetime.now(timezone.utc).isoformat()
    mapping["schema_version"] = 2
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


def process_markets(mapping_file: Path):
    print("Fetching markets from Gamma API...")
    payloads = []
    for url in GAMMA_SEARCH_URLS:
        try:
            payloads.append(fetch_json(url))
        except OSError as exc:
            raise SystemExit(f"Gamma fetch failed: {exc}")

    mapping = load_mapping(mapping_file)
    existing_keys = collect_existing_keys(mapping)
    city_cache = {}
    incoming_keys = set()
    added = skipped = 0
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

                for k in market_keys(entry):
                    incoming_keys.add(k)

                if is_duplicate(existing_keys, entry):
                    skipped += 1
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

                if cache_key in city_cache:
                    city_feature = city_cache[cache_key]
                else:
                    try:
                        city_feature = resolve_city_feature(city_name, rules_texts)
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
                    target = resolve_capture_target(entry.get("title", "?"), target_texts)
                    if target:
                        entry["target"] = target
                    else:
                        unresolved_by_type["capture_no_target"] = (
                            unresolved_by_type.get("capture_no_target", 0) + 1
                        )
                        print(f"  Skip [capture] '{entry.get('title', '?')}': no target")
                        continue

                if add_market(mapping, existing_keys, entry, city_payload, geometry, market_type):
                    added += 1
                else:
                    skipped += 1

    # Auto-remove markets no longer in the active Gamma feed
    removed = 0
    for city_entry in mapping.get("cities", {}).values():
        for mtype, mlist in list((city_entry.get("markets") or {}).items()):
            updated = []
            for m in mlist:
                if any(k in incoming_keys for k in market_keys(m)):
                    updated.append(m)
                else:
                    title = m.get("title") or m.get("slug") or m.get("id") or "?"
                    print(f"  Removing stale market '{title}'")
                    removed += 1
            city_entry["markets"][mtype] = updated

    # Remove cities with no remaining markets
    empty = [cid for cid, ce in mapping.get("cities", {}).items()
             if all(not v for v in (ce.get("markets") or {}).values())]
    for cid in empty:
        name = (mapping["cities"][cid].get("city") or {}).get("name_en") or cid
        print(f"  Removing city with no markets: {name}")
        del mapping["cities"][cid]

    save_mapping(mapping_file, mapping)
    total_unresolved = sum(unresolved_by_type.values())
    print(f"\nDone: +{added} added, {skipped} skipped, {removed} removed, "
          f"{total_unresolved} unresolved.")
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
    process_markets(mapping_file)


if __name__ == "__main__":
    main()
