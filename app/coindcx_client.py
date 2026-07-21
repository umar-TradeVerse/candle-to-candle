"""
CoinDCX Futures API client — Candle-to-Candle.

Adapted directly from the tested TradeVerse coindcx_client.py (same account, same
proven request/response handling), scoped down to this bot's two symbols and its
simpler needs (no 15m/daily-trend helpers, no wallet-balance dependency).

Key mechanism (carried over as-is, it's better than what I originally wrote):
  - SL is placed AS PART of the entry order via `stop_loss_price` — no separate
    stop order to manage.
  - Ratcheting the SL later is a single `update_stop_loss()` call against the
    open position, not a cancel+recreate of a resting order.
  - Quantity/price rounding to the exchange's tick size is handled internally via
    cached instrument details.
"""

import asyncio
import logging
import math
import time
import hmac
import hashlib
import json
from typing import Optional
import aiohttp
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger("coindcx")
IST = pytz.timezone("Asia/Kolkata")

COINDCX_BASE_URL = "https://api.coindcx.com"
COINDCX_PUBLIC_URL = "https://public.coindcx.com"

# This bot only ever trades these two — kept separate from TradeVerse's SYMBOL_MAP
# so the two bots can never accidentally collide on a symbol key.
# *** CORRECTED 2026-07-21 *** — confirmed live: "B-XAU_USDT" does not exist
# ("Invalid pair" error). The underlying token is XAUT (Tether Gold), matching
# CoinDCX's spot pair "XAUTUSDT" — so the futures pair follows the same naming
# convention as BTC. Still worth a live confirmation on first successful entry.
SYMBOL_MAP = {
    "BTC": "B-BTC_USDT",
    "GOLD": "B-XAUT_USDT",
}
REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}

# *** FLAGGED FOR REVIEW ***
# TradeVerse's tested client never set this (its pairs are USDT-margined and just used
# the account's implicit default). Since this bot specifically wants INR margin, we set
# it explicitly here — but this exact param name/behavior is NOT proven by TradeVerse's
# live usage. Verify on first dry-run that positions actually open INR-margined, not USDT.
MARGIN_CURRENCY_SHORT_NAME = "INR"


class CoinDCXClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = COINDCX_BASE_URL
        self.public_url = COINDCX_PUBLIC_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_error: Optional[str] = None
        self._instrument_cache: dict = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _generate_signature(self, body: str) -> str:
        secret_bytes = bytes(self.api_secret, encoding="utf-8")
        return hmac.new(secret_bytes, body.encode(), hashlib.sha256).hexdigest()

    async def _post(self, endpoint: str, data: dict) -> Optional[dict]:
        session = await self._get_session()
        url = self.base_url + endpoint
        json_body = json.dumps(data, separators=(",", ":"))
        signature = self._generate_signature(json_body)
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
        }
        try:
            async with session.post(url, data=json_body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201):
                    self.last_error = None
                    return await resp.json()
                text = await resp.text()
                logger.error(f"POST {endpoint} -> {resp.status}: {text}")
                self.last_error = text
                return None
        except asyncio.TimeoutError:
            logger.error(f"POST {endpoint} timeout")
            self.last_error = "Request timed out"
            return None
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
            self.last_error = str(e)
            return None

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        session = await self._get_session()
        url = self.public_url + path
        try:
            async with session.get(url, params=params,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.error(f"GET {path} -> {resp.status}: {text}")
                return None
        except asyncio.TimeoutError:
            logger.error(f"GET {path} timeout")
            return None
        except Exception as e:
            logger.error(f"GET {path} error: {e}")
            return None

    async def _get_base(self, path: str, params: dict = None) -> Optional[dict]:
        session = await self._get_session()
        url = self.base_url + path
        try:
            async with session.get(url, params=params,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.error(f"GET {path} -> {resp.status}: {text}")
                return None
        except asyncio.TimeoutError:
            logger.error(f"GET {path} timeout")
            return None
        except Exception as e:
            logger.error(f"GET {path} error: {e}")
            return None

    # ---------- market data ----------
    async def get_usdt_inr_rate(self) -> Optional[float]:
        """
        *** CRITICAL FIX 2026-07-21 ***
        Needed because compute_quantity() was treating POSITION_SIZE_INR (rupees) as
        if it were already in USDT before dividing by a USDT-denominated price — with
        USDT trading around ₹100+, that inflated every position by ~90-100x, which is
        exactly what produced "Insufficient funds" on a wallet that actually had plenty
        of INR. This fetches the real rate so sizing converts INR -> USDT correctly
        before computing quantity.
        """
        session = await self._get_session()
        url = self.base_url + "/exchange/ticker"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"GET /exchange/ticker -> {resp.status}")
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"GET /exchange/ticker error: {e}")
            return None

        if not isinstance(data, list):
            return None
        for entry in data:
            if entry.get("market") == "USDTINR":
                try:
                    return float(entry["last_price"])
                except (KeyError, ValueError, TypeError):
                    return None
        logger.error("USDTINR market not found in ticker response")
        return None

    async def get_futures_candles(self, symbol: str, interval: str = "4h",
                                   lookback_days: int = 5, limit: int = 60) -> list:
        """
        CoinDCX's public candles endpoint does NOT support a native 4h interval —
        confirmed live: only 1m, 15m, 1h, 1d are accepted (error BFF-SO-004 otherwise).
        For interval="4h" we fetch 1h candles and aggregate them into synthetic 4H
        candles ourselves, bucketed on UTC 4-hour boundaries (00, 04, 08, 12, 16, 20).
        This lines up EXACTLY with the IST candle-open times this strategy uses —
        01:30/05:30/09:30/13:30/17:30/21:30 IST = 20:00/00:00/04:00/08:00/12:00/16:00
        UTC — so no approximation is involved, just correct bucketing.

        Returns complete candle list (oldest first), including the still-forming
        latest (possibly-partial) 4H candle.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return []

        fetch_interval = "1h" if interval == "4h" else interval
        fetch_limit = limit * 4 if interval == "4h" else limit

        now_ist = datetime.now(IST)
        start_ts = int((now_ist - timedelta(days=lookback_days)).timestamp() * 1000)
        end_ts = int(now_ist.timestamp() * 1000)
        params = {"pair": coindcx_symbol, "interval": fetch_interval, "from": start_ts,
                  "to": end_ts, "limit": fetch_limit}

        result = await self._get("/market_data/candles", params=params)
        if not result or not isinstance(result, list):
            logger.error(f"{symbol} | No {fetch_interval} candle data returned")
            return []

        raw = []
        for c in result:
            try:
                raw.append({
                    "open_time": int(c["time"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        raw.sort(key=lambda c: c["open_time"])

        if interval != "4h":
            return raw
        return self._aggregate_to_4h(raw)

    @staticmethod
    def _aggregate_to_4h(hourly_candles: list) -> list:
        BUCKET_MS = 4 * 3600 * 1000
        buckets: dict = {}
        for c in hourly_candles:
            bucket_key = c["open_time"] // BUCKET_MS
            buckets.setdefault(bucket_key, []).append(c)

        aggregated = []
        for bucket_key in sorted(buckets.keys()):
            group = sorted(buckets[bucket_key], key=lambda c: c["open_time"])
            aggregated.append({
                "open_time": group[0]["open_time"],
                "open": group[0]["open"],
                "high": max(c["high"] for c in group),
                "low": min(c["low"] for c in group),
                "close": group[-1]["close"],
            })
        return aggregated

    # ---------- instrument details / rounding ----------
    async def _get_instrument_details(self, coindcx_symbol: str) -> Optional[dict]:
        if coindcx_symbol in self._instrument_cache:
            return self._instrument_cache[coindcx_symbol]

        result = await self._get_base(
            "/exchange/v1/derivatives/futures/data/instrument",
            params={"pair": coindcx_symbol}
        )
        if not result or "instrument" not in result:
            logger.warning(f"{coindcx_symbol} | Could not fetch instrument details — "
                           f"quantity will NOT be rounded to step size")
            return None

        inst = result["instrument"]
        if inst.get("pair") != coindcx_symbol:
            logger.warning(f"{coindcx_symbol} | Instrument details mismatch — ignoring")
            return None

        details = {
            "quantity_increment": float(inst.get("quantity_increment", 0) or 0),
            "min_quantity": float(inst.get("min_quantity", 0) or 0),
            "min_notional": float(inst.get("min_notional", 0) or 0),
            "price_increment": float(inst.get("price_increment", 0) or 0),
        }
        self._instrument_cache[coindcx_symbol] = details
        logger.info(f"{coindcx_symbol} | Instrument details cached: {details}")
        return details

    @staticmethod
    def _round_to_increment(quantity: float, increment: float, mode: str = "floor") -> float:
        if not increment or increment <= 0:
            return quantity
        if mode == "nearest":
            steps = round(quantity / increment)
        else:
            steps = math.floor(quantity / increment + 1e-9)
        rounded = steps * increment
        inc_str = f"{increment:.10f}".rstrip("0")
        decimals = len(inc_str.split(".")[1]) if "." in inc_str else 0
        return round(rounded, decimals)

    # ---------- trading ----------
    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                  sl_price: float, leverage: int = 5) -> Optional[dict]:
        """Single call: enters the position AND attaches the stop-loss. No separate
        stop order to track."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["quantity_increment"] > 0:
            original_quantity = quantity
            quantity = self._round_to_increment(quantity, instrument["quantity_increment"])
            if quantity != original_quantity:
                logger.info(f"{symbol} | Quantity rounded {original_quantity} -> {quantity}")

            if quantity <= 0 or (instrument["min_quantity"] and quantity < instrument["min_quantity"]):
                logger.error(f"{symbol} | Rounded quantity {quantity} below minimum "
                             f"{instrument['min_quantity']} — order not sent")
                return {"id": None, "error": f"Quantity {quantity} below exchange minimum "
                                              f"{instrument['min_quantity']} after rounding"}

            if instrument["price_increment"] > 0:
                original_sl = sl_price
                sl_price = self._round_to_increment(sl_price, instrument["price_increment"], mode="nearest")
                if sl_price != original_sl:
                    logger.info(f"{symbol} | SL rounded {original_sl} -> {sl_price}")

        timestamp = int(time.time() * 1000)
        body = {
            "timestamp": timestamp,
            "order": {
                "side": side.lower(),
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "leverage": leverage,
                "stop_loss_price": sl_price,
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False,
                "margin_currency_short_name": MARGIN_CURRENCY_SHORT_NAME,
            },
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)

        if not result and self.last_error and "not active" in self.last_error.lower():
            # *** SELF-DIAGNOSING FALLBACK 2026-07-21 ***
            # Working theory: some instruments (newer tokenized-commodity listings like
            # gold) may not yet support INR-margined trading specifically, even though
            # they render under the app's "INR Futures" tab. Retry once without forcing
            # INR margin — if THIS succeeds, we've learned something concrete instead of
            # guessing, and the caller is told clearly rather than this proceeding silently.
            logger.warning(f"{symbol} | Order rejected as 'not active' with INR margin — "
                           f"retrying once without forcing margin currency")
            body["order"].pop("margin_currency_short_name", None)
            body["timestamp"] = int(time.time() * 1000)
            result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
            if result:
                order_obj = result[0] if isinstance(result, list) else result
                order_id = order_obj.get("id")
                logger.warning(f"{symbol} | Order succeeded WITHOUT forced INR margin — "
                               f"this instrument likely doesn't support INR margin yet, "
                               f"verify actual margin currency on CoinDCX manually")
                return {"id": order_id, "symbol": symbol, "side": side, "quantity": quantity,
                         "sl_price": sl_price, "leverage": leverage,
                         "warning": "Entered WITHOUT forced INR margin — instrument may not support it"}

        if result:
            if isinstance(result, list) and not result:
                logger.error(f"{symbol} | Empty list response from order create — verify manually")
                return {"id": None, "error": "Empty list response — verify manually"}
            order_obj = result[0] if isinstance(result, list) else result
            order_id = order_obj.get("id")
            logger.info(f"{symbol} {side} market order placed with SL @ {sl_price} "
                        f"leverage={leverage}x: {order_id}")
            return {"id": order_id, "symbol": symbol, "side": side, "quantity": quantity,
                     "sl_price": sl_price, "leverage": leverage}

        logger.error(f"{symbol} | Failed to place market order with SL: {self.last_error}")
        return {"id": None, "error": self.last_error or "Unknown error"}

    async def get_open_positions(self) -> dict:
        """Returns {internal_symbol: active_pos} for currently-open positions only."""
        timestamp = int(time.time() * 1000)
        result = await self._post("/exchange/v1/derivatives/futures/positions", {"timestamp": timestamp})

        entries = result if isinstance(result, list) else (result.get("positions", []) if result else [])
        positions = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pair = entry.get("pair")
            try:
                active = float(entry.get("active_pos", 0) or 0)
            except (TypeError, ValueError):
                active = 0.0
            internal_symbol = REVERSE_SYMBOL_MAP.get(pair)
            if internal_symbol and active != 0:
                positions[internal_symbol] = active
        return positions

    async def get_position_details(self, symbol: str) -> Optional[dict]:
        """Open position + manually-computed ROE (verified against CoinDCX's own UI)."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return None

        timestamp = int(time.time() * 1000)
        result = await self._post("/exchange/v1/derivatives/futures/positions", {"timestamp": timestamp})
        entries = result if isinstance(result, list) else []

        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            try:
                active = float(entry.get("active_pos", 0) or 0)
            except (TypeError, ValueError):
                active = 0.0
            if active == 0:
                continue
            try:
                avg_price = float(entry.get("avg_price", 0) or 0)
                mark_price = float(entry.get("mark_price", 0) or 0)
                margin = float(entry.get("locked_user_margin", 0) or 0)
            except (TypeError, ValueError):
                return {"id": entry.get("id"), "active_pos": active, "roe": None, "raw": entry}

            if margin <= 0:
                return {"id": entry.get("id"), "active_pos": active, "roe": None, "raw": entry}

            pnl = (mark_price - avg_price) * active if active > 0 else (avg_price - mark_price) * abs(active)
            roe = (pnl / margin) * 100
            return {"id": entry.get("id"), "active_pos": active, "roe": roe,
                     "pnl": pnl, "mark_price": mark_price, "avg_price": avg_price, "raw": entry}
        return None

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> bool:
        """side = the ORIGINAL entry side ('BUY' or 'SELL'); flips internally to close."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return False

        close_side = "sell" if side.upper() == "BUY" else "buy"
        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["quantity_increment"] > 0:
            quantity = self._round_to_increment(quantity, instrument["quantity_increment"])

        timestamp = int(time.time() * 1000)
        body = {
            "timestamp": timestamp,
            "order": {
                "side": close_side,
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False,
                "margin_currency_short_name": MARGIN_CURRENCY_SHORT_NAME,
                # reduce_only intentionally omitted — CoinDCX rejects it on market
                # orders. Caller must pass an accurate, freshly-fetched quantity.
            },
        }
        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            logger.info(f"{symbol} | Position closed via {close_side} market order (qty {quantity})")
            return True
        logger.error(f"{symbol} | Failed to close position: {self.last_error}")
        return False

    async def update_stop_loss(self, symbol: str, new_sl_price: float) -> bool:
        """Ratchets the SL by updating the position's TP/SL directly — no cancel/recreate."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, cannot update SL")
            return False

        timestamp = int(time.time() * 1000)
        positions_result = await self._post("/exchange/v1/derivatives/futures/positions", {"timestamp": timestamp})
        entries = positions_result if isinstance(positions_result, list) else []

        position_id = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            try:
                active = float(entry.get("active_pos", 0) or 0)
            except (TypeError, ValueError):
                active = 0.0
            if active != 0:
                position_id = entry.get("id")
                break

        if not position_id:
            logger.error(f"{symbol} | Could not find an open position id — SL not updated")
            return False

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["price_increment"] > 0:
            new_sl_price = self._round_to_increment(new_sl_price, instrument["price_increment"], mode="nearest")

        body = {
            "timestamp": timestamp,
            "id": position_id,
            "stop_loss": {"stop_price": new_sl_price, "limit_price": new_sl_price, "order_type": "stop_market"},
        }
        result = await self._post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
        if result:
            logger.info(f"{symbol} | Stop-loss updated to {new_sl_price} (position {position_id})")
            return True
        logger.error(f"{symbol} | Failed to update stop-loss: {self.last_error}")
        return False
