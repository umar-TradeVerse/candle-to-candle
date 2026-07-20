"""
Candle-to-Candle — main orchestration.

Built against the tested CoinDCXClient (adapted from TradeVerse's live client):
  - Entry + SL are ONE call: place_market_order(symbol, side, qty, sl_price, leverage)
  - Ratcheting SL is ONE call: update_stop_loss(symbol, new_sl_price)
  - No separate stop-order ids to track/cancel — the position IS the SL carrier.

*** FLAGGED FOR REVIEW *** (see README) — INR margin param, GOLD instrument details,
and one supervised dry run all still need a live check before real capital.
"""
import asyncio
import logging
from datetime import datetime

from app.config import (
    IST, TRADING_WEEKDAYS, LEVERAGE, POSITION_SIZE_INR, SL_BUFFER_PCT,
    ANCHOR_CANDLE_HOUR_IST, ANCHOR_CANDLE_MINUTE_IST,
    FRIDAY_FORCE_CLOSE_HOUR_IST, FRIDAY_FORCE_CLOSE_MINUTE_IST,
    POLL_INTERVAL_SECONDS, CANDLE_INTERVAL,
    COINDCX_API_KEY, COINDCX_API_SECRET,
)
from app.coindcx_client import CoinDCXClient, SYMBOL_MAP
from app.strategy import Candle, DayContext, check_breakout, initial_sl, ratchet_sl
from app.state_store import load_state, save_state, get_symbol_state, set_symbol_state
from app.telegram_bot import TelegramBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

client = CoinDCXClient(COINDCX_API_KEY, COINDCX_API_SECRET)


def to_candle(d) -> Candle:
    return Candle(open_time=d["open_time"], open=d["open"], high=d["high"], low=d["low"], close=d["close"])


def is_trading_day(dt) -> bool:
    return dt.weekday() in TRADING_WEEKDAYS


def is_past_friday_force_close(dt) -> bool:
    if dt.weekday() != 4:
        return False
    cutoff = dt.replace(hour=FRIDAY_FORCE_CLOSE_HOUR_IST, minute=FRIDAY_FORCE_CLOSE_MINUTE_IST,
                         second=0, microsecond=0)
    return dt >= cutoff


class SymbolWorker:
    def __init__(self, name, bot: TelegramBot):
        self.name = name  # "BTC" or "GOLD" — matches coindcx_client.SYMBOL_MAP keys
        self.bot = bot
        self.last_seen_candle_open = None

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

    def find_anchor_candle(self, candles, now):
        for c in candles:
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
            anchor = self.find_anchor_candle(candles, now)
            if anchor is not None:
                sym_state["date_str"] = today_str
                sym_state["anchor_high"] = anchor.high
                sym_state["anchor_low"] = anchor.low
                sym_state["traded_today"] = False
                sym_state["stopped_out_today"] = False
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

        if last_closed.open_time == self.last_seen_candle_open:
            state = set_symbol_state(state, self.name, sym_state)
            save_state(state)
            return
        self.last_seen_candle_open = last_closed.open_time

        if ctx.position_side:
            new_sl = ratchet_sl(ctx.current_sl, ctx.position_side, last_closed, SL_BUFFER_PCT)
            if new_sl != ctx.current_sl:
                ok = await client.update_stop_loss(self.name, new_sl)
                if ok:
                    sym_state["current_sl"] = new_sl
                    await self.bot.send_alert(f"[{self.name}] SL ratcheted to {new_sl:.2f}")
                else:
                    await self.bot.send_alert(f"[{self.name}] WARNING: SL ratchet failed, check exchange manually")
        elif not ctx.traded_today and not ctx.stopped_out_today and ctx.anchor_high:
            direction = check_breakout(ctx, last_closed)
            if direction:
                await self.enter(ctx, direction, last_closed, sym_state)

        state = set_symbol_state(state, self.name, sym_state)
        save_state(state)

    async def enter(self, ctx, direction, trigger_candle, sym_state):
        side = "buy" if direction == "long" else "sell"
        qty = self.compute_quantity(trigger_candle.close)
        sl_price = initial_sl(ctx, direction, trigger_candle, SL_BUFFER_PCT)
        result = await client.place_market_order(self.name, side, qty, sl_price, LEVERAGE)
        if result and result.get("id"):
            sym_state["traded_today"] = True
            sym_state["position_side"] = direction
            sym_state["current_sl"] = sl_price
            await self.bot.send_alert(
                f"[{self.name}] ENTERED {direction.upper()} at ~{trigger_candle.close:.2f}, SL={sl_price:.2f}"
            )
        else:
            err = result.get("error") if result else "no response"
            logger.error(f"[{self.name}] entry failed: {err}")
            await self.bot.send_alert(f"[{self.name}] ENTRY FAILED ({err}) — check exchange manually")

    def compute_quantity(self, price):
        # Rounding to the instrument's step size happens inside place_market_order.
        return (POSITION_SIZE_INR * LEVERAGE) / price

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
            return f"{self.name}: flat"
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

    bot = TelegramBot(status_fn, close_fn)
    for name in SYMBOL_MAP.keys():
        workers[name] = SymbolWorker(name, bot)

    await bot.start_polling()
    for w in workers.values():
        await w.reconcile()
    await bot.send_alert("Candle-to-Candle bot started.")

    try:
        while True:
            now = datetime.now(IST)
            for w in workers.values():
                try:
                    await w.run_once(now)
                except Exception:
                    logger.exception(f"[{w.name}] run_once failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await client.close()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
