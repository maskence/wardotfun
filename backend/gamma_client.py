import json
import logging
import time
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
CACHE_TTL = 60  # seconds

_cache: dict[str, tuple[float, dict | None]] = {}  # condition_id → (timestamp, result)


def get_market_odds(condition_id: str) -> dict | None:
    """
    Fetch YES/NO odds for a market by conditionId.
    Returns {"yes": 0.73, "no": 0.27} or None on failure.
    Results are cached for CACHE_TTL seconds.
    """
    now = time.time()
    cached = _cache.get(condition_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        url = f"{GAMMA_BASE}/{condition_id}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception as exc:
        logger.warning("Failed to fetch odds for %s: %s", condition_id, exc)
        _cache[condition_id] = (now, None)
        return None

    result = _parse_odds(data)
    _cache[condition_id] = (now, result)
    return result


def _parse_odds(data: dict | list) -> dict | None:
    # Gamma may return a single market object or a list
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None

    outcomes = data.get("outcomes")
    prices = data.get("outcomePrices")

    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != len(prices):
        return None

    result = {}
    for outcome, price in zip(outcomes, prices):
        key = str(outcome).lower()
        try:
            result[key] = round(float(price), 4)
        except (ValueError, TypeError):
            return None

    if "yes" not in result or "no" not in result:
        return None

    return result
