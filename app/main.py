"""
Candle-to-Candle — main orchestration.

*** STRATEGY REDESIGNED 2026-07-28 *** — entry is now confirmation-based on 15m
candles (Candle1 touch -> Candle2 confirm -> entry on break of Candle2's high/low),
replacing the old "any 4H candle closing beyond the OR triggers immediate entry".
Post-entry management (4H ratchet) is UNCHANGED. See strategy.py's module docstring
for the full new entry spec.

Built against the tested CoinDCXClient (adapted from TradeVerse's live client):
  - Entry + SL are TWO calls: place_market_order() then update_stop_loss()
  - No separate stop-order ids to track/cancel — the position IS the SL carrier.

BTC-only as of 2026-07-22 (GOLD dropped — see README "Why GOLD was dropped").
"""
import asyncio
import logging
from datetime import datetime

from app.config import (
    IST, TRADING_WEEKDAYS, TRADES_WEEKENDS, LEVERAGE, POSITION_SIZE_INR, SL_BUFFER_PCT,
    ANCHOR_CANDLE_HOUR_IST, ANCHOR_CANDLE_MINUTE_IST,
    FRIDAY_FORCE_CLOSE_HOUR_IST, FRIDAY_FORCE_CLOSE_MINUTE_IST,
    POLL_INTERVAL_SECONDS, CANDLE_INTERVAL,
    HEARTBEAT_HOUR_IST, HEARTBEAT_MINUTE_IST,
    ENTRY_RETRY_BACKOFF_SECONDS,
    COINDCX_API_KEY, COINDCX_API_SECRET,
)
from app.coindcx_client import CoinDCXClient, SYMBOL_MAP
from app.strategy import (
    Candle, DayContext, ratchet_sl,
    detect_touch, check_confirmation, check_setup_invalidated, check_entry_trigger,
    initial_sl_from_candle2,
)
from app.state_store import load_state, save_state, get_symbol_state, set_symbol_state
from app.telegram_bot import TelegramBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# httpx logs the full request URL at INFO level — for Telegram API calls that URL
# embeds the bot token (https://api.telegram.org/bot<TOKEN>/method). Silencing this
# specific logger to WARNING stops the token from ever appearing in logs, without
# losing anything useful (our own coindcx/main loggers carry the actual signal).
# NOTE: only httpx needs this — python-telegram-bot's own logger ("telegram") never
# includes the token in its messages, so it's left at INFO to keep useful startup
# confirmations like "Application started" visible.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("main")

client = CoinDCXClient(COINDCX_API_KEY, COINDCX_API_SECRET)

# Simple in-memory health tracking, exposed via /health and the daily heartbeat.
# (Deliberately NOT persisted — if the process restarts, "bot_start_time" resetting
# to "now" is itself useful information, not something to hide.)
_health = {
    "bot_start_time": None,
    "last_poll_at": None,
    "last_poll_ok": None,
    "heartbeat_sent_date": None,
}


def to_candle(d) -> Candle:
    return Candle(open_time=d["open_time"], open=d["open"], high=d["high"], low=d["low"], close=d["close"])


def is_trading_day(dt) -> bool:
    return dt.weekday() in TRADING_WEEKDAYS


def is_past_friday_force_close(dt) -> bool:
    if TRADES_WEEKENDS:
        return False  # no weekend gap to protect against if Sat/Sun are trading days
    if dt.weekday() != 4:
        return False
    cutoff = dt.replace(hour=FRIDAY_FORCE_CLOSE_HOUR_IST, minute=FRIDAY_FORCE_CLOSE_MINUTE_IST,
                         second=0, microsecond=0)
    return dt >= cutoff


class SymbolWorker:
    def __init__(self, name, bot: TelegramBot):
        self.name = name  # e.g. "BTC" — matches a key in coindcx_client.SYMBOL_MAP
        self.bot = bot
        self.last_seen_candle_open = None      # gates 4H candle processing (ratchet)
        self.last_seen_15m_candle_open = None  # gates 15m candle processing (pre-entry scan)

    async def reconcile(self):
        """On startup: truth comes from the exchange, never assumed from local state."""
        details = await client.get_position_details(self.name)
        state = load_state()
        sym_state = get_symbol_state(state, self.name)

        if details and details.get("active_pos"):
            side = "long" if details["active_pos"] > 0 else "short"
            sym_state["position_side"] = side
            sym_state["traded_today"] = True
            logger.info(f"[{self.name}] reconciled: open {side} position found on exchange")
        else:
            sym_state["position_side"] = None
            sym_state["current_sl"] = None

        state = set_symbol_state(state, self.name, sym_state)
        save_state(state)

    async def sync_position_state(self, sym_state):
        """Every cycle, not just startup: if we think a position is open but the
        exchange shows flat, the SL fired (or it was closed some other way) —
        treat it as a stop-out and alert, instead of trusting stale local state."""
        if not sym_state.get("position_side"):
            return sym_state
        open_positions = await client.get_open_positions()
        if self.name not in open_positions:
            logger.info(f"[{self.name}] position closed on exchange (SL fill or external close)")
            sym_state["position_side"] = None
            sym_state["current_sl"] = None
            sym_state["stopped_out_today"] = True
            await self.bot.send_alert(f"[{self.name}] Position closed on exchange (stop hit). No re-entry today.")
        return sym_state

    def find_anchor_candle(self, closed_candles, now):
        """
        closed_candles must exclude the still-forming candle — the anchor range is only
        valid once the 01:30 IST candle has FULLY closed (i.e. at 05:30), never from a
        partial/in-progress version of it.
        """
        for c in closed_candles:
            c_time = datetime.fromtimestamp(c.open_time / 1000, tz=IST)
            if (c_time.hour == ANCHOR_CANDLE_HOUR_IST and c_time.minute == ANCHOR_CANDLE_MINUTE_IST
                    and c_time.date() == now.date()):
                return c
        return None

    async def run_once(self, now):
        state = load_state()
        sym_state = get_symbol_state(state, self.name)
        today_str = now.strftime("%Y-%m-%d")

        sym_state = await self.sync_position_state(sym_state)
        state = set_symbol_state(state, self.name, sym_state)
        save_state(state)

        if sym_state.get("position_side") and is_past_friday_force_close(now):
            await self.force_close(sym_state, "weekend force-close")
            state = set_symbol_state(state, self.name, sym_state)
            save_state(state)
            return

        if not is_trading_day(now):
            return  # Mon/weekend: no new setups; open trades keep trailing until Friday cutoff

        candles_raw = await client.get_futures_candles(self.name, interval=CANDLE_INTERVAL, limit=30)
        if len(candles_raw) < 2:
            return
        candles = [to_candle(c) for c in candles_raw]
        last_closed = candles[-2]  # last item is the still-forming candle

        if sym_state.get("date_str") != today_str:
            anchor = self.find_anchor_candle(candles[:-1], now)  # exclude still-forming candle
            if anchor is not None:
                sym_state["date_str"] = today_str
                sym_state["anchor_high"] = anchor.high
                sym_state["anchor_low"] = anchor.low
                sym_state["traded_today"] = False
                sym_state["stopped_out_today"] = False
                sym_state["phase"] = "waiting_touch"
                sym_state["candle1"] = None
                sym_state["candle2"] = None
                await self.bot.send_alert(f"[{self.name}] New day range set: H={anchor.high} L={anchor.low}")

        ctx = DayContext(
            date_str=sym_state.get("date_str", today_str),
            anchor_high=sym_state.get("anchor_high", 0),
            anchor_low=sym_state.get("anchor_low", 0),
            traded_today=sym_state.get("traded_today", False),
            stopped_out_today=sym_state.get("stopped_out_today", False),
            position_side=sym_state.get("position_side"),
            current_sl=sym_state.get("current_sl"),
        )

        # Urgent, candle-independent check: a position with no SL is a live risk and
        # can't wait for the next "new candle" cycle to fix it. This runs EVERY poll
        # (every ~30s), deliberately placed before the same-candle early-return below.
        if ctx.position_side and ctx.current_sl is None:
            target_sl = sym_state.get("pending_sl")
            if target_sl:
                ok = await client.update_stop_loss(self.name, target_sl)
                if ok:
                    sym_state["current_sl"] = target_sl
                    await self.bot.send_alert(f"[{self.name}] SL now attached: {target_sl:.2f} (was previously missing)")
                else:
                    last_protect_alert = sym_state.get("last_sl_protect_alert_at")
                    seconds_since = (now.timestamp() - last_protect_alert) if last_protect_alert else None
                    if seconds_since is None or seconds_since >= ENTRY_RETRY_BACKOFF_SECONDS:
                        sym_state["last_sl_protect_alert_at"] = now.timestamp()
                        await self.bot.send_alert(
                            f"🚨 [{self.name}] STILL UNPROTECTED — SL attach retrying every poll cycle, "
                            f"consider setting one manually on CoinDCX or /close {self.name}"
                        )
            state = set_symbol_state(state, self.name, sym_state)
            save_state(state)
            return

        if last_closed.open_time == self.last_seen_candle_open:
            state = set_symbol_state(state, self.name, sym_state)
            save_state(state)
            return

        if ctx.position_side:
            new_sl = ratchet_sl(ctx.current_sl, ctx.position_side, last_closed, SL_BUFFER_PCT)
            candle_extreme = last_closed.low if ctx.position_side == "long" else last_closed.high
            logger.info(
                f"[{self.name}] ratchet check @ candle close {last_closed.open_time}: "
                f"side={ctx.position_side} candle_extreme={candle_extreme:.2f} "
                f"current_sl={ctx.current_sl:.2f} candidate_sl={new_sl:.2f} "
                f"{'WILL TIGHTEN' if new_sl != ctx.current_sl else 'no improvement, unchanged'}"
            )
            if new_sl != ctx.current_sl:
                ok = await client.update_stop_loss(self.name, new_sl)
                if ok:
                    sym_state["current_sl"] = new_sl
                    await self.bot.send_alert(f"[{self.name}] SL ratcheted to {new_sl:.2f}")
                    self.last_seen_candle_open = last_closed.open_time
                else:
                    await self.bot.send_alert(
                        f"[{self.name}] WARNING: SL ratchet failed, will retry next poll cycle"
                    )
                    # deliberately NOT advancing last_seen_candle_open — retry until it succeeds
                    # or the position closes some other way (sync_position_state handles that)
            else:
                self.last_seen_candle_open = last_closed.open_time  # nothing to ratchet, mark handled
        elif not ctx.traded_today and not ctx.stopped_out_today and ctx.anchor_high:
            self.last_seen_candle_open = last_closed.open_time  # 4H tracker still advances; pre-entry uses 15m
            await self.handle_pre_entry(sym_state, ctx, now)
        else:
            self.last_seen_candle_open = last_closed.open_time  # nothing actionable this cycle

        state = set_symbol_state(state, self.name, sym_state)
        save_state(state)

    async def handle_pre_entry(self, sym_state, ctx, now):
        """
        Confirmation-based entry state machine, operating on 15m candles (unlike the
        4H candles used for the OR itself and the post-entry ratchet). Phases:
        waiting_touch -> waiting_confirmation -> waiting_entry -> entering -> (done)
        """
        phase = sym_state.get("phase", "waiting_touch")
        anchor_high, anchor_low = ctx.anchor_high, ctx.anchor_low

        if phase == "entering":
            # Trigger already fired — retry entry with backoff regardless of new candles,
            # same pattern as the old entry retry (indefinite, bounded only by day end).
            last_attempt = sym_state.get("last_entry_attempt_at")
            seconds_since = (now.timestamp() - last_attempt) if last_attempt else None
            if seconds_since is not None and seconds_since < ENTRY_RETRY_BACKOFF_SECONDS:
                return
            candle1, candle2 = sym_state.get("candle1"), sym_state.get("candle2")
            if not candle1 or not candle2:
                logger.error(f"[{self.name}] phase=entering but candle1/candle2 missing — resetting to scan")
                sym_state["phase"] = "waiting_touch"
                return
            sym_state["last_entry_attempt_at"] = now.timestamp()
            entered = await self.enter_confirmed(candle1["direction"], candle2, sym_state)
            if entered:
                sym_state["phase"] = None
                sym_state["candle1"] = None
                sym_state["candle2"] = None
            return

        # waiting_touch / waiting_confirmation / waiting_entry all gate on a NEW 15m candle
        candles_raw = await client.get_futures_candles(self.name, interval="15m", lookback_days=1, limit=100)
        if len(candles_raw) < 2:
            return
        candles_15m = [to_candle(c) for c in candles_raw]
        last_closed_15m = candles_15m[-2]  # last item is still-forming

        if sym_state.get("last_seen_15m_open") == last_closed_15m.open_time:
            return
        sym_state["last_seen_15m_open"] = last_closed_15m.open_time

        if phase == "waiting_touch":
            direction = detect_touch(anchor_high, anchor_low, last_closed_15m)
            if direction:
                sym_state["candle1"] = {
                    "open_time": last_closed_15m.open_time,
                    "high": last_closed_15m.high, "low": last_closed_15m.low,
                    "direction": direction,
                }
                sym_state["phase"] = "waiting_confirmation"
                logger.info(f"[{self.name}] Candle1 touch detected ({direction}) @ {last_closed_15m.open_time}")

        elif phase == "waiting_confirmation":
            candle1 = sym_state.get("candle1")
            direction = candle1["direction"]
            if check_confirmation(anchor_high, anchor_low, direction, last_closed_15m):
                sym_state["candle2"] = {
                    "open_time": last_closed_15m.open_time,
                    "high": last_closed_15m.high, "low": last_closed_15m.low,
                }
                sym_state["phase"] = "waiting_entry"
                trigger_level = last_closed_15m.high if direction == "long" else last_closed_15m.low
                await self.bot.send_alert(
                    f"[{self.name}] Candle2 confirmed {direction.upper()} — watching for a close "
                    f"beyond {trigger_level:.2f} to enter"
                )
            else:
                # Failed confirmation (retest or no close beyond) — discard, scan fresh.
                # Does NOT consume the day's one-trade allowance.
                sym_state["phase"] = "waiting_touch"
                sym_state["candle1"] = None

        elif phase == "waiting_entry":
            candle1, candle2 = sym_state.get("candle1"), sym_state.get("candle2")
            direction = candle1["direction"]
            if check_setup_invalidated(anchor_high, anchor_low, direction, last_closed_15m):
                sym_state["phase"] = "waiting_touch"
                sym_state["candle1"] = None
                sym_state["candle2"] = None
                await self.bot.send_alert(f"[{self.name}] Setup invalidated (gave back the move) — rescanning")
                return
            if check_entry_trigger(candle2["high"], candle2["low"], direction, last_closed_15m):
                sym_state["phase"] = "entering"
                sym_state["last_entry_attempt_at"] = now.timestamp()
                entered = await self.enter_confirmed(direction, candle2, sym_state)
                if entered:
                    sym_state["phase"] = None
                    sym_state["candle1"] = None
                    sym_state["candle2"] = None

    async def enter_confirmed(self, direction, candle2, sym_state):
        side = "buy" if direction == "long" else "sell"
        usdt_inr_rate = await client.get_usdt_inr_rate()
        if not usdt_inr_rate or usdt_inr_rate <= 0:
            logger.error(f"[{self.name}] could not fetch USDT/INR rate — aborting entry rather than risk mis-sizing")
            await self.bot.send_alert(f"[{self.name}] ENTRY BLOCKED: couldn't fetch USDT/INR rate, will retry")
            return False
        sl_price = initial_sl_from_candle2(direction, candle2["low"], candle2["high"], SL_BUFFER_PCT)
        ref_price = candle2["high"] if direction == "long" else candle2["low"]
        qty = self.compute_quantity(ref_price, usdt_inr_rate)
        result = await client.place_market_order(self.name, side, qty, sl_price, LEVERAGE)
        sym_state["pending_sl"] = sl_price
        if result and result.get("id"):
            sym_state["traded_today"] = True
            sym_state["position_side"] = direction
            if result.get("sl_attach_failed"):
                sym_state["current_sl"] = None
                await self.bot.send_alert(
                    f"🚨 [{self.name}] ENTERED {direction.upper()} but SL ATTACHMENT FAILED — position "
                    f"is UNPROTECTED. Check CoinDCX now, or /close {self.name}."
                )
            else:
                sym_state["current_sl"] = sl_price
                await self.bot.send_alert(
                    f"{self.name} {direction.upper()}\n"
                    f"Opening Range: H={sym_state.get('anchor_high')} L={sym_state.get('anchor_low')}\n"
                    f"SL: {sl_price:.2f} (from Candle2 {'low' if direction == 'long' else 'high'})\n"
                    f"Trade Status: ACTIVE"
                )
            return True
        else:
            err = result.get("error") if result else "no response"
            logger.error(f"[{self.name}] entry failed: {err}")
            await self.bot.send_alert(
                f"[{self.name}] ENTRY FAILED ({err}) — will retry in {ENTRY_RETRY_BACKOFF_SECONDS // 60} min"
            )
            return False

    def compute_quantity(self, price_usdt, usdt_inr_rate):
        """
        POSITION_SIZE_INR is rupees, LEVERAGE gives notional exposure, but the pair's
        price is quoted in USDT — so INR must convert to USDT before dividing by price.
        Rounding to the instrument's step size happens inside place_market_order.
        """
        notional_inr = POSITION_SIZE_INR * LEVERAGE
        notional_usdt = notional_inr / usdt_inr_rate
        return notional_usdt / price_usdt

    async def force_close(self, sym_state, reason):
        details = await client.get_position_details(self.name)
        if details and details.get("active_pos"):
            side = "BUY" if details["active_pos"] > 0 else "SELL"
            qty = abs(details["active_pos"])
            ok = await client.close_position_market(self.name, side, qty)
            if not ok:
                await self.bot.send_alert(f"[{self.name}] WARNING: force-close failed, check exchange manually")
                return
        await self.bot.send_alert(f"[{self.name}] Position closed ({reason})")
        sym_state["position_side"] = None
        sym_state["current_sl"] = None

    async def status_line(self):
        details = await client.get_position_details(self.name)
        if not details or not details.get("active_pos"):
            state = load_state()
            sym_state = get_symbol_state(state, self.name)
            phase = sym_state.get("phase", "waiting_touch")
            anchor_high, anchor_low = sym_state.get("anchor_high"), sym_state.get("anchor_low")
            phase_label = {
                "waiting_touch": "waiting for Candle1 touch",
                "waiting_confirmation": "waiting for Candle2 confirmation",
                "waiting_entry": "confirmed, watching for entry trigger",
                "entering": "entry trigger fired, placing order",
                None: "no active setup",
            }.get(phase, phase)
            or_str = f" | OR: H={anchor_high} L={anchor_low}" if anchor_high else ""
            return f"{self.name}: flat ({phase_label}){or_str}"
        roe = details.get("roe")
        roe_str = f"{roe:.2f}%" if roe is not None else "n/a"
        return (f"{self.name}: {'LONG' if details['active_pos'] > 0 else 'SHORT'} "
                f"{abs(details['active_pos'])} @ {details.get('avg_price')} "
                f"| mark={details.get('mark_price')} | ROE={roe_str}")

    async def manual_close(self):
        state = load_state()
        sym_state = get_symbol_state(state, self.name)
        await self.force_close(sym_state, "manual /close")
        state = set_symbol_state(state, self.name, sym_state)
        save_state(state)
        return f"{self.name}: closed."


async def main():
    workers = {}

    async def status_fn():
        return "\n".join([await w.status_line() for w in workers.values()])

    async def close_fn(target):
        if target == "all":
            return "\n".join([await w.manual_close() for w in workers.values()])
        return await workers[target].manual_close()

    async def health_fn():
        now = datetime.now(IST)
        started = _health["bot_start_time"]
        last_poll = _health["last_poll_at"]
        uptime_min = int((now - started).total_seconds() / 60) if started else 0
        since_poll_sec = int((now - last_poll).total_seconds()) if last_poll else None
        poll_status = "OK" if _health["last_poll_ok"] else "FAILED (check logs)"
        lines = [
            f"Started: {started.strftime('%Y-%m-%d %H:%M IST') if started else 'unknown'} (up {uptime_min} min)",
            f"Last poll: {since_poll_sec}s ago — {poll_status}" if since_poll_sec is not None else "No poll completed yet",
        ]
        return "\n".join(lines)

    bot = TelegramBot(status_fn, close_fn, valid_symbols=SYMBOL_MAP.keys(), health_fn=health_fn)
    for name in SYMBOL_MAP.keys():
        workers[name] = SymbolWorker(name, bot)

    await bot.start_polling()
    for w in workers.values():
        await w.reconcile()
    _health["bot_start_time"] = datetime.now(IST)
    await bot.send_alert("Candle-to-Candle bot started.")

    try:
        while True:
            now = datetime.now(IST)
            poll_ok = True
            for w in workers.values():
                try:
                    await w.run_once(now)
                except Exception:
                    logger.exception(f"[{w.name}] run_once failed")
                    poll_ok = False
            _health["last_poll_at"] = now
            _health["last_poll_ok"] = poll_ok

            # Once-a-day heartbeat so a silent overnight crash doesn't go unnoticed.
            if (now.hour, now.minute) >= (HEARTBEAT_HOUR_IST, HEARTBEAT_MINUTE_IST) \
                    and _health["heartbeat_sent_date"] != now.date():
                text = await health_fn()
                await bot.send_alert(f"Daily heartbeat:\n{text}")
                _health["heartbeat_sent_date"] = now.date()

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await client.close()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
