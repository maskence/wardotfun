import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_MAPPING_FILE = Path(__file__).resolve().parents[1] / "data" / "city_market_map.json"
DEFAULT_BACKTEST_FILE = Path(__file__).resolve().parents[1] / "data" / "backtest_city_market_map.json"
SETTLEMENTS_LAYER_URL = (
    "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
    "Ukrainian_Settlements_Updated_view/FeatureServer/0/query"
)
GAMMA_SEARCH_URLS_LIVE = [
    "https://gamma-api.polymarket.com/public-search?q=russia%20enter&events_status=active&limit_per_type=200&page=1&keep_closed_markets=0",
    "https://gamma-api.polymarket.com/public-search?q=russia%20capture&events_status=active&limit_per_type=200&page=1&keep_closed_markets=0",
]
GAMMA_SEARCH_URLS_BACKTEST = [
    "https://gamma-api.polymarket.com/public-search?q=russia%20enter&limit_per_type=400&page=1&keep_closed_markets=1",
    "https://gamma-api.polymarket.com/public-search?q=russia%20enter&limit_per_type=400&page=2&keep_closed_markets=1",
    "https://gamma-api.polymarket.com/public-search?q=russia%20capture&limit_per_type=400&page=1&keep_closed_markets=1",
    "https://gamma-api.polymarket.com/public-search?q=russia%20capture&limit_per_type=400&page=2&keep_closed_markets=1",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build a city-market mapping from Gamma public-search output."
    )
    parser.add_argument(
        "--mapping-file",
        default=None,
        type=Path,
        help="Path to city-market mapping JSON.",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Build a backtest mapping with resolved market data (includes closed markets).",
    )
    return parser.parse_args()


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request) as response:
        return json.load(response)


def parse_json_list(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return parsed
    return []


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
    if isinstance(markets, list):
        return markets
    return []


def extract_deadline(market, event):
    for key in (
        "endDate",
        "closeTime",
        "closedTime",
        "closeDate",
        "closeDateTime",
        "endTime",
        "resolutionTime",
    ):
        value = market.get(key) or event.get(key)
        if value:
            return value
    return None


def market_display_name(market, event):
    return (
        market.get("question")
        or market.get("title")
        or event.get("title")
        or market.get("slug")
        or market.get("id")
        or "unknown"
    )


def compute_resolved(market):
    outcomes = parse_json_list(market.get("outcomes"))
    prices = parse_json_list(market.get("outcomePrices"))
    is_closed = market.get("closed") is True
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    try:
        yes_index = outcomes.index("Yes")
        yes_price = float(prices[yes_index])
    except (ValueError, IndexError):
        return None
    if not is_closed:
        return None
    if yes_price == 1.0:
        return True
    if yes_price == 0.0:
        return False
    return None


def build_market_entry(market, event, backtest=False):
    market_id = market.get("id")
    condition_id = market.get("conditionId")
    token_ids = parse_json_list(market.get("clobTokenIds"))
    outcomes = parse_json_list(market.get("outcomes"))
    fees_enabled = market.get("feesEnabled")
    fee_type = market.get("feeType")
    if fees_enabled is not False:
        raise SystemExit(
            f"Market {market_display_name(market, event)!r} has feesEnabled={fees_enabled!r}; expected false."
        )
    if fee_type is not None:
        raise SystemExit(
            f"Market {market_display_name(market, event)!r} has feeType={fee_type!r}; expected null."
        )
    if not market_id or not condition_id or len(token_ids) < 2:
        return None
    tick_size = market.get("orderPriceMinTickSize")
    if tick_size is None:
        raise SystemExit(
            f"Market {market_display_name(market, event)!r} missing orderPriceMinTickSize."
        )
    entry = {
        "id": str(market_id),
        "conditionId": condition_id,
        "clobTokenIds": token_ids,
        "outcomes": outcomes,
        "title": market.get("question") or market.get("title") or event.get("title"),
        "slug": market.get("slug"),
        "deadline": extract_deadline(market, event),
        "eventId": str(event.get("id")) if event.get("id") is not None else None,
        "tickSize": format(float(tick_size), "g"),
        "negRisk": bool(market.get("negRisk")),
        "feeRateBps": 0,
    }
    if backtest:
        entry["_resolved"] = compute_resolved(market)
        closed_time = market.get("closedTime") or market.get("closedAt") or market.get("closed_time")
        if closed_time:
            entry["_closed_at"] = closed_time
    return {key: value for key, value in entry.items() if value is not None}


def extract_coordinates(text):
    if not text:
        return None
    patterns = [
        r"(?P<lat>-?\d+\.\d+)\s*[°º]?\s*[Nn].*?"
        r"(?P<lon>-?\d+\.\d+)\s*[°º]?\s*[Ee]",
        r"lat(?:itude)?\s*[:=]\s*(?P<lat>-?\d+\.\d+).*?"
        r"lon(?:gitude)?\s*[:=]\s*(?P<lon>-?\d+\.\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            lat = float(match.group("lat"))
            lon = float(match.group("lon"))
            return lat, lon
    match = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", text)
    if match:
        lat = float(match.group(1))
        lon = float(match.group(2))
        if abs(lat) > 90 and abs(lon) <= 90:
            lat, lon = lon, lat
        if abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    return None


def find_coordinates(texts):
    for text in texts:
        coords = extract_coordinates(text or "")
        if coords:
            return coords
    return None


def normalize_city_name(name):
    if not name:
        return None
    cleaned = name.replace("’", "'")
    cleaned = re.sub(r"\s*\(.*?\)", "", cleaned)
    cleaned = cleaned.split(",", 1)[0].strip()
    cleaned = re.sub(r"\b(once\s+)?again\b$", "", cleaned, flags=re.IGNORECASE).strip()
    lowered = cleaned.lower()
    for prefix in (
        "the city of ",
        "city of ",
        "the town of ",
        "town of ",
        "the village of ",
        "village of ",
    ):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def slug_to_title(slug):
    if not slug:
        return None
    return slug.replace("-", " ").replace("_", " ").strip()


def infer_market_type(text):
    if not text:
        return None
    lowered = text.lower()
    if "capture all" in lowered:
        return "capture_all"
    if "capture" in lowered:
        return "capture"
    if "enter" in lowered:
        return "enter"
    return None


def infer_market_type_from_texts(texts):
    for text in texts:
        market_type = infer_market_type(text or "")
        if market_type:
            return market_type
    return None


def extract_city_name(text):
    if not text:
        return None
    patterns = [
        r"capture all(?: of)?\s+(.*?)\s+by",
        r"capture\s+(.*?)\s+by",
        r"enter\s+(.*?)\s+by",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def extract_city_from_description(text):
    if not text:
        return None
    patterns = [
        r"territory of\s+([^,]+)",
        r"in the city of\s+([^,]+)",
        r"in\s+([^,]+)\s+oblast",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def extract_city_name_from_texts(texts):
    for text in texts:
        city = extract_city_name(text or "")
        if city:
            return city
    for text in texts:
        city = extract_city_from_description(text or "")
        if city:
            return city
    return None


def project_to_mercator(lat, lon):
    radius = 6378137.0
    x = (lon * math.pi / 180.0) * radius
    y = (math.log(math.tan(math.pi / 4.0 + (lat * math.pi / 360.0)))) * radius
    return x, y


def build_target_payload(lat, lon):
    x, y = project_to_mercator(lat, lon)
    return {
        "type": "point",
        "sr": 3857,
        "geometry": [x, y],
        "source": {"lat": lat, "lon": lon},
    }


def first_coordinate(geometry):
    if not geometry:
        return None
    rings = geometry.get("rings")
    if not rings:
        return None
    return rings[0][0] if rings[0] else None


def project_to_lonlat(point):
    if not point:
        return None
    x, y = point
    radius = 6378137.0
    lon = (x / radius) * (180 / math.pi)
    lat = (2 * math.atan(math.exp(y / radius)) - math.pi / 2) * (180 / math.pi)
    return [lon, lat]


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
    params.append("outSR=3857")
    params.append("spatialRel=esriSpatialRelIntersects")
    params.append(
        "outFields=ADM4_EN,ADM4_UA,ADM3_EN,ADM3_UA,ADM2_EN,ADM2_UA,ADM1_EN,ADM1_UA,OBJECTID,GlobalID"
    )
    params.append("returnGeometry=true")
    params.append("cacheHint=true")
    params.append("f=json")
    url = f"{SETTLEMENTS_LAYER_URL}?" + "&".join(params)
    data = fetch_json(url)
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        code = error.get("code")
        message = error.get("message") or "ArcGIS query failed."
        details = "; ".join(error.get("details") or [])
        if details:
            message = f"{message} {details}"
        if code == 429:
            raise SystemExit(f"ArcGIS rate limit hit (429). {message}")
        raise SystemExit(f"ArcGIS query error ({code}): {message}")
    return data.get("features", [])


def choose_feature(features, label, interactive=True):
    if len(features) == 1:
        return features[0]
    print(f"Found {len(features)} matches for {label}:")
    for index, feature in enumerate(features):
        attrs = feature.get("attributes", {})
        coord = first_coordinate(feature.get("geometry"))
        lonlat = project_to_lonlat(coord)
        coord_str = f"{lonlat[0]:.6f}, {lonlat[1]:.6f}" if lonlat else "unknown"
        print(
            f"[{index}] ADM4_EN={attrs.get('ADM4_EN')} "
            f"ADM3_EN={attrs.get('ADM3_EN')} ADM2_EN={attrs.get('ADM2_EN')} "
            f"ADM1_EN={attrs.get('ADM1_EN')} first_coord={coord_str}"
        )

    if not interactive:
        print(f"Auto-selecting first match for '{label}'.")
        return features[0]

    while True:
        selection = input("Select index to use (or 's' to skip): ").strip().lower()
        if selection == "s":
            return None
        try:
            choice = int(selection)
        except ValueError:
            print("Please enter a number or 's'.")
            continue
        if 0 <= choice < len(features):
            return features[choice]
        print("Index out of range.")


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
        safe_name = variant.replace("'", "''")
        for field in ("ADM4_EN", "ADM4_UA"):
            exact_where = f"UPPER({field})='{safe_name.upper()}'"
            features = query_settlement(where=exact_where)
            if features:
                return features
    for variant in build_name_variants(city_name):
        safe_name = variant.replace("'", "''")
        for field in ("ADM4_EN", "ADM4_UA"):
            like_where = f"UPPER({field}) LIKE '%{safe_name.upper()}%'"
            features = query_settlement(where=like_where)
            if features:
                return features
    return []


def resolve_city_feature(city_name, text_candidates, interactive=True):
    coords = find_coordinates(text_candidates)
    if coords:
        lat, lon = coords
        features = query_settlement(
            geometry=f"{lon},{lat}",
            geometry_type="esriGeometryPoint",
            in_sr=4326,
        )
        if features:
            selected = choose_feature(features, f"coordinates {lat:.5f},{lon:.5f}", interactive=interactive)
            if selected:
                return selected, coords
        else:
            print(f"No settlement found for coordinates {lat},{lon}; trying name.")

    features = query_city_by_name(city_name)
    if not features and not interactive:
        return None, coords
    while not features:
        fallback = input(
            f"No settlement found for '{city_name}'. "
            "Enter alternate name or 's' to skip: "
        ).strip()
        if fallback.lower() == "s":
            return None, coords
        if not fallback:
            continue
        city_name = fallback
        features = query_city_by_name(city_name)
    selected = choose_feature(features, city_name, interactive=interactive)
    return selected, coords


def build_city_payload(feature):
    attrs = feature.get("attributes", {})
    return {
        "name_en": attrs.get("ADM4_EN"),
        "name_ua": attrs.get("ADM4_UA"),
        "adm4_en": attrs.get("ADM4_EN"),
        "adm4_ua": attrs.get("ADM4_UA"),
        "adm3_en": attrs.get("ADM3_EN"),
        "adm3_ua": attrs.get("ADM3_UA"),
        "adm2_en": attrs.get("ADM2_EN"),
        "adm2_ua": attrs.get("ADM2_UA"),
        "adm1_en": attrs.get("ADM1_EN"),
        "adm1_ua": attrs.get("ADM1_UA"),
        "object_id": attrs.get("OBJECTID"),
        "global_id": attrs.get("GlobalID"),
    }


def load_mapping(path: Path):
    if not path.exists():
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cities": {},
        }
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cities": {},
        }
    cities = payload.get("cities")
    if not isinstance(cities, dict):
        raise SystemExit("Mapping file missing 'cities' dict; rebuild with new schema.")
    for city_entry in cities.values():
        if isinstance(city_entry.get("markets"), list):
            raise SystemExit(
                "Legacy mapping file detected (markets list). Use a fresh mapping file."
            )
    payload.setdefault("schema_version", 1)
    payload.setdefault("cities", {})
    return payload


def save_mapping(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    tmp_path.replace(path)


def collect_existing_keys(mapping):
    keys = set()
    empty_targets = 0
    for city_entry in mapping.get("cities", {}).values():
        markets = city_entry.get("markets") or {}
        for market_list in markets.values():
            for market in market_list:
                market_id = market.get("id")
                if market_id:
                    keys.add(f"id:{market_id}")
                slug = market.get("slug")
                if slug:
                    keys.add(f"slug:{slug}")
        for market in markets.get("capture", []) or []:
            if not market.get("target"):
                empty_targets += 1
    return keys, empty_targets


def market_lookup_keys(market):
    keys = []
    market_id = market.get("id")
    slug = market.get("slug")
    if market_id:
        keys.append(f"id:{market_id}")
    if slug:
        keys.append(f"slug:{slug}")
    return keys


def is_duplicate_market(existing_keys, market_entry):
    for key in market_lookup_keys(market_entry):
        if key in existing_keys:
            return True
    return False


def is_market_resolved(market, event):
    for flag in ("resolved", "isResolved", "isResolvedMarket"):
        if market.get(flag) is True or event.get(flag) is True:
            return True
    if market.get("closed") is True or event.get("closed") is True:
        return True
    status = market.get("status") or event.get("status")
    if isinstance(status, str) and status.lower() in {"resolved", "closed"}:
        return True
    return False


def prompt_market_removal(title, reason):
    while True:
        response = input(f"Market '{title}' {reason}. Delete? [y/N]: ").strip().lower()
        if response in {"y", "yes"}:
            return True
        if response in {"", "n", "no"}:
            return False
        print("Please enter y or n.")


def add_market(
    mapping,
    existing_keys,
    market_entry,
    city_payload,
    geometry,
    market_type,
    target=None,
):
    market_id = market_entry.get("id")
    slug = market_entry.get("slug")
    title = market_entry.get("title") or slug or market_id or "unknown"
    if is_duplicate_market(existing_keys, market_entry):
        print(f"Skipping market '{title}': already exists.")
        return False

    cities = mapping.setdefault("cities", {})
    city_id = city_payload.get("global_id")
    if not city_id:
        raise SystemExit("City payload missing global_id.")

    city_entry = cities.get(city_id)
    if not city_entry:
        city_entry = {
            "city": city_payload,
            "geometry": geometry,
            "markets": {"enter": [], "capture": [], "capture_all": []},
        }
        cities[city_id] = city_entry
    else:
        city_entry.setdefault("city", city_payload)
        if geometry and not city_entry.get("geometry"):
            city_entry["geometry"] = geometry
        city_entry.setdefault("markets", {"enter": [], "capture": [], "capture_all": []})

    markets = city_entry["markets"]
    market_list = markets.setdefault(market_type, [])
    entry = dict(market_entry)
    if target:
        entry["target"] = target
    elif market_type == "capture" and "target" not in entry:
        entry["target"] = {}
    market_list.append(entry)

    if market_id:
        existing_keys.add(f"id:{market_id}")
    if slug:
        existing_keys.add(f"slug:{slug}")
    city_name = city_payload.get("name_en") or "unknown"
    print(f"Added market '{title}' for {city_name}.")
    return True


def process_markets(args, backtest=False):
    gamma_urls = GAMMA_SEARCH_URLS_BACKTEST if backtest else GAMMA_SEARCH_URLS_LIVE
    payloads = []
    for url in gamma_urls:
        try:
            payloads.append(fetch_json(url))
        except OSError as exc:
            raise SystemExit(f"Failed to fetch Gamma data from {url}: {exc}")

    mapping_file = args.mapping_file
    if mapping_file is None:
        mapping_file = DEFAULT_BACKTEST_FILE if backtest else DEFAULT_MAPPING_FILE

    mapping = load_mapping(mapping_file)
    existing_keys, empty_targets = collect_existing_keys(mapping)
    city_cache = {}
    added = 0
    skipped = 0
    unresolved = 0
    removed = 0
    incoming_index = {}

    for payload in payloads:
        for event in iter_events(payload):
            for market in iter_markets(event):
                market_title = market_display_name(market, event)
                resolved_flag = is_market_resolved(market, event)
                for key in market_lookup_keys(market):
                    incoming_index[key] = {"title": market_title, "resolved": resolved_flag}
                if not backtest and market.get("closed"):
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
                    print(f"Skipping market '{market_title}': unknown type")
                    continue

                market_entry = build_market_entry(market, event, backtest=backtest)
                if not market_entry:
                    print(f"Skipping market '{market_title}': missing ids or tokens")
                    continue
                if is_duplicate_market(existing_keys, market_entry):
                    print(f"Skipping market '{market_title}': already exists.")
                    skipped += 1
                    continue

                city_name = extract_city_name_from_texts(text_candidates)
                if not city_name:
                    city_name = extract_city_name_from_texts(
                        [
                            market.get("rules"),
                            market.get("description"),
                            event.get("rules"),
                            event.get("description"),
                        ]
                    )
                if not city_name:
                    unresolved += 1
                    print(f"Failed to resolve market '{market_title}': no city match")
                    continue

                rules_texts = [
                    market.get("rules"),
                    market.get("description"),
                    event.get("rules"),
                    event.get("description"),
                ]
                coords = find_coordinates(rules_texts)
                cache_key = (normalize_city_name(city_name) or city_name or "", coords)

                if cache_key in city_cache:
                    city_feature, coords = city_cache[cache_key]
                else:
                    try:
                        city_feature, coords = resolve_city_feature(
                            city_name, rules_texts, interactive=not backtest,
                        )
                    except SystemExit as exc:
                        unresolved += 1
                        print(f"Failed to resolve market '{market_title}': {exc}")
                        continue
                    if city_feature is None:
                        unresolved += 1
                        print(f"Failed to resolve market '{market_title}': user skipped")
                        continue
                    city_cache[cache_key] = (city_feature, coords)

                city_payload = build_city_payload(city_feature)
                geometry = city_feature.get("geometry")

                target = None
                if market_type == "capture" and coords:
                    target = build_target_payload(*coords)
                    print(f"Resolved target for capture market '{market_title}'.")
                elif market_type == "capture":
                    empty_targets += 1
                    print(f"No target found for capture market '{market_title}'.")

                if add_market(
                    mapping,
                    existing_keys,
                    market_entry,
                    city_payload,
                    geometry,
                    market_type,
                    target=target,
                ):
                    added += 1
                else:
                    skipped += 1

    if not backtest:
        for city_entry in mapping.get("cities", {}).values():
            markets = city_entry.get("markets") or {}
            for market_type, market_list in list(markets.items()):
                updated_list = []
                for market in market_list:
                    keys = market_lookup_keys(market)
                    title = market.get("title") or market.get("slug") or market.get("id") or "unknown"
                    if not keys:
                        updated_list.append(market)
                        continue
                    status = None
                    for key in keys:
                        status = incoming_index.get(key)
                        if status:
                            break
                    reason = None
                    if status and status.get("resolved"):
                        reason = "is resolved or closed in latest query results"
                    elif not status:
                        reason = "is missing from latest query results"
                    if reason and prompt_market_removal(title, reason):
                        removed += 1
                        if market_type == "capture" and not market.get("target"):
                            empty_targets = max(0, empty_targets - 1)
                        continue
                    updated_list.append(market)
                markets[market_type] = updated_list

        cities = mapping.get("cities", {})
        empty_city_ids = []
        for city_id, city_entry in cities.items():
            markets = city_entry.get("markets") or {}
            if all(not (market_list or []) for market_list in markets.values()):
                empty_city_ids.append(city_id)
        for city_id in empty_city_ids:
            city_payload = cities[city_id].get("city") or {}
            city_name = city_payload.get("name_en") or city_payload.get("name_ua") or city_id
            print(f"Removing city with no markets: {city_name}")
            del cities[city_id]

    save_mapping(mapping_file, mapping)

    if backtest:
        resolved_yes = 0
        resolved_no = 0
        resolved_none = 0
        for city_entry in mapping.get("cities", {}).values():
            for market_list in (city_entry.get("markets") or {}).values():
                for market in market_list:
                    r = market.get("_resolved")
                    if r is True:
                        resolved_yes += 1
                    elif r is False:
                        resolved_no += 1
                    else:
                        resolved_none += 1
        print(
            f"Added {added} markets. Skipped {skipped} duplicates. "
            f"Skipped {unresolved} unresolved cities. "
            f"Resolved YES: {resolved_yes}. Resolved NO: {resolved_no}. "
            f"Unresolved/active: {resolved_none}. "
            f"Capture markets with empty targets: {empty_targets}."
        )
    else:
        print(
            f"Added {added} markets. Skipped {skipped} duplicates. "
            f"Skipped {unresolved} unresolved cities. "
            f"Removed {removed} markets. "
            f"Capture markets with empty targets: {empty_targets}."
        )


def main():
    args = parse_args()
    process_markets(args, backtest=args.backtest)


if __name__ == "__main__":
    main()
