import json
import logging
import pickle
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from . import city_map
except ImportError:
    import city_map

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/batch-prices-history"
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
CACHE_PATH = Path(__file__).parent / "data" / "mapper_cache" / "polymarket.pkl"


def _request_json(url: str, payload: dict | list | None = None) -> dict | list:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; wardotfun/1.0)",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _decode_list(value) -> list:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PolymarketDataService:
    snapshot_ttl = 60
    history_ttl = 15 * 60

    def __init__(self):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._payload: dict = {
            "markets": {}, "resolved_markets": [], "status": "loading", "updated_at": None,
        }
        self._history_cache: dict[str, tuple[float, list[dict]]] = {}

    def start(self):
        if not CACHE_PATH.exists():
            return
        try:
            payload = pickle.loads(CACHE_PATH.read_bytes())
            if not isinstance(payload, dict):
                return
            payload["resolved_markets"] = self._resolved_from_archive()
            with self._lock:
                self._payload = payload
                cached_at = float(payload.get("updated_at") or 0)
                for market in payload.get("markets", {}).values():
                    token_id = market.get("token_id")
                    if token_id:
                        self._history_cache[token_id] = (cached_at, market.get("history") or [])
            logger.info("Loaded Polymarket cache: %d markets", len(payload.get("markets", {})))
        except Exception as exc:
            logger.warning("Failed to load Polymarket cache: %s", exc)

    def get_data(self) -> dict:
        now = time.time()
        with self._lock:
            if ("resolved_markets" in self._payload
                    and self._payload.get("updated_at")
                    and now - self._payload["updated_at"] < self.snapshot_ttl):
                return self._payload

        with self._refresh_lock:
            with self._lock:
                if ("resolved_markets" in self._payload
                        and self._payload.get("updated_at")
                        and now - self._payload["updated_at"] < self.snapshot_ttl):
                    return self._payload
            try:
                payload = self._refresh(now)
            except Exception as exc:
                logger.warning("Polymarket refresh failed: %s", exc)
                with self._lock:
                    if self._payload.get("markets"):
                        return {**self._payload, "status": "stale", "error": str(exc)}
                return {"markets": {}, "status": "error", "updated_at": None, "error": str(exc)}

            with self._lock:
                self._payload = payload
            try:
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_bytes(pickle.dumps(payload))
            except OSError as exc:
                logger.warning("Failed to save Polymarket cache: %s", exc)
            return payload

    def invalidate(self):
        with self._lock:
            self._payload["updated_at"] = None
            self._payload["resolved_markets"] = self._resolved_from_archive()

    def _refresh(self, now: float) -> dict:
        references = self._market_references()
        remote_markets = self._fetch_gamma_markets(list(references))
        token_ids = []
        open_token_ids = []
        normalized = {}

        for market_id, local_market in references.items():
            remote = remote_markets.get(market_id)
            if not remote:
                continue
            outcomes = [str(value).lower() for value in _decode_list(remote.get("outcomes"))]
            prices = [_float(value) for value in _decode_list(remote.get("outcomePrices"))]
            tokens = [str(value) for value in _decode_list(remote.get("clobTokenIds"))]
            yes_index = outcomes.index("yes") if "yes" in outcomes else 0
            no_index = outcomes.index("no") if "no" in outcomes else 1
            yes = prices[yes_index] if yes_index < len(prices) else _float(remote.get("lastTradePrice"))
            no = prices[no_index] if no_index < len(prices) else (1 - yes if yes is not None else None)
            token_id = tokens[yes_index] if yes_index < len(tokens) else None
            if token_id:
                token_ids.append(token_id)
            if token_id and not remote.get("closed"):
                open_token_ids.append(token_id)
            normalized[market_id] = {
                "id": market_id,
                "condition_id": remote.get("conditionId") or local_market.get("conditionId"),
                "yes": yes,
                "no": no,
                "token_id": token_id,
                "volume": _float(remote.get("volumeNum") or remote.get("volume")),
                "closed": bool(remote.get("closed")),
                "updated_at": remote.get("updatedAt"),
                "history": [],
                "orderbook": None,
            }

        histories = self._get_histories(token_ids, now)
        orderbooks = self._get_orderbooks(open_token_ids)
        for market in normalized.values():
            if market["token_id"]:
                market["history"] = histories.get(market["token_id"], [])
                market["orderbook"] = orderbooks.get(market["token_id"])

        missing = len(references) - len(normalized)
        resolved_markets = self._resolved_from_archive()
        status = "ok" if not missing else "partial"
        logger.info(
            "Polymarket refreshed: %d markets, %d resolved, %d missing",
            len(normalized), len(resolved_markets), missing,
        )
        return {
            "markets": normalized,
            "resolved_markets": resolved_markets,
            "status": status,
            "updated_at": now,
            "missing_market_count": missing,
        }

    def _market_references(self) -> dict[str, dict]:
        references = {}
        for entry in city_map.get().get("cities", {}).values():
            for markets in entry.get("markets", {}).values():
                for market in markets or []:
                    resolved = market.get("status") == "resolved"
                    if (not self._is_active(market) and not resolved) or not market.get("id"):
                        continue
                    references[str(market["id"])] = market
        return references

    @staticmethod
    def _is_active(market: dict) -> bool:
        status = market.get("status")
        return status == "active" if status else market.get("active") is not False

    def _resolved_from_archive(self) -> list[dict]:
        resolved = []
        for city_id, entry in city_map.get().get("cities", {}).items():
            city_name = (entry.get("city") or {}).get("name_en") or "Unknown"
            for market_type, markets in entry.get("markets", {}).items():
                for market in markets or []:
                    if market.get("status") != "resolved":
                        continue
                    resolved.append({
                        "id": str(market.get("id") or ""),
                        "title": market.get("title"),
                        "slug": market.get("slug"),
                        "event_slug": market.get("eventSlug"),
                        "city_id": city_id,
                        "city_name": city_name,
                        "type": market_type,
                        "deadline": market.get("deadline"),
                        "resolved_at": market.get("resolvedAt"),
                        "outcome": market.get("outcome"),
                    })
        return sorted(
            resolved,
            key=lambda market: self._date_timestamp(market.get("resolved_at")),
            reverse=True,
        )

    @staticmethod
    def _date_timestamp(value) -> float:
        if not value:
            return 0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0

    def _fetch_gamma_markets(self, market_ids: list[str]) -> dict[str, dict]:
        markets = {}
        self._fetch_gamma_chunks(market_ids, markets)
        missing_ids = [market_id for market_id in market_ids if market_id not in markets]
        self._fetch_gamma_chunks(missing_ids, markets, closed=True)
        return markets

    def _fetch_gamma_chunks(self, market_ids: list[str], markets: dict[str, dict], closed: bool = False):
        for offset in range(0, len(market_ids), 100):
            chunk = market_ids[offset:offset + 100]
            query = urlencode([("id", market_id) for market_id in chunk])
            query += f"&limit={len(chunk)}"
            if closed:
                query += "&closed=true"
            response = _request_json(f"{GAMMA_MARKETS_URL}?{query}")
            for market in response if isinstance(response, list) else []:
                if market.get("id") is not None:
                    markets[str(market["id"])] = market

    def _get_histories(self, token_ids: list[str], now: float) -> dict[str, list[dict]]:
        unique_tokens = list(dict.fromkeys(token_ids))
        histories = {}
        missing = []
        with self._lock:
            for token_id in unique_tokens:
                cached = self._history_cache.get(token_id)
                if cached and now - cached[0] < self.history_ttl:
                    histories[token_id] = cached[1]
                else:
                    missing.append(token_id)

        for offset in range(0, len(missing), 20):
            chunk = missing[offset:offset + 20]
            try:
                response = _request_json(CLOB_HISTORY_URL, {
                    "markets": chunk,
                    "interval": "1m",
                    "fidelity": 360,
                })
                response_histories = response.get("history", {}) if isinstance(response, dict) else {}
            except Exception as exc:
                logger.warning("Polymarket history batch failed: %s", exc)
                response_histories = {}
            with self._lock:
                for token_id in chunk:
                    points = self._normalize_history(response_histories.get(token_id, []))
                    if not points and token_id in self._history_cache:
                        points = self._history_cache[token_id][1]
                    else:
                        self._history_cache[token_id] = (now, points)
                    histories[token_id] = points
        return histories

    def _get_orderbooks(self, token_ids: list[str]) -> dict[str, dict]:
        with self._lock:
            previous = {
                market["token_id"]: market["orderbook"]
                for market in self._payload.get("markets", {}).values()
                if market.get("token_id") and market.get("orderbook")
            }
        if not token_ids:
            return previous
        try:
            response = _request_json(CLOB_BOOKS_URL, [
                {"token_id": token_id} for token_id in dict.fromkeys(token_ids)
            ])
        except Exception as exc:
            logger.warning("Polymarket orderbook refresh failed: %s", exc)
            return previous

        orderbooks = dict(previous)
        for book in response if isinstance(response, list) else []:
            token_id = str(book.get("asset_id") or "")
            if token_id:
                orderbooks[token_id] = self._normalize_orderbook(book)
        return orderbooks

    @staticmethod
    def _normalize_orderbook(book: dict) -> dict:
        def levels(side: str, reverse: bool) -> list[dict]:
            parsed = []
            for level in book.get(side, []):
                price = _float(level.get("price")) if isinstance(level, dict) else None
                size = _float(level.get("size")) if isinstance(level, dict) else None
                if price is not None and size is not None:
                    parsed.append({"price": price, "size": size})
            return sorted(parsed, key=lambda level: level["price"], reverse=reverse)

        return {
            "bids": levels("bids", True),
            "asks": levels("asks", False),
            "timestamp": book.get("timestamp"),
            "last_trade_price": _float(book.get("last_trade_price")),
        }

    @staticmethod
    def _normalize_history(points: list) -> list[dict]:
        normalized = []
        for point in points if isinstance(points, list) else []:
            timestamp = _float(point.get("t")) if isinstance(point, dict) else None
            price = _float(point.get("p")) if isinstance(point, dict) else None
            if timestamp is None or price is None:
                continue
            normalized.append({"t": int(timestamp), "p": price})
        return normalized
