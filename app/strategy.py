"""
Candle-to-Candle strategy — pure logic, no exchange calls, no indicators.

Rules (confirmed spec — updated 2026-07-22, full week / BTC only):
1. Each trading day (full week, Mon-Sun IST — originally Tue-Fri only when this ran
   both BTC and GOLD; GOLD has since been dropped and BTC trades 24/7, so there's no
   calendar constraint left), the 01:30 IST 4H candle closes -> mark its high/low
   as the day's reference range.
2. Watch every subsequent candle that day for a close beyond that range.
   - Close above range-high  -> LONG
   - Close below range-low   -> SHORT
   - If a single candle's wick pierces BOTH sides, direction is decided by where the
     CLOSE ends up. If close lands back inside the range, no trigger.
3. Initial SL = opposite extreme of the anchor (01:30) candle, extended by SL_BUFFER_PCT
   further out (so a wick-touch of the raw level doesn't stop us out).
4. On every later candle close, ratchet the SL to that candle's low (long) / high (short),
   again extended by SL_BUFFER_PCT.
5. No re-entry same day after being stopped out.
6. Trade can run across multiple days; only exit is the trailing SL or a manual /close.
   (The old mandatory Friday-evening force-close no longer applies now that weekends
   are trading days too — see config.py's TRADES_WEEKENDS.)
"""
from dataclasses import dataclass, field
from typing import Optional, Literal
from decimal import Decimal, ROUND_DOWN

Side = Literal["long", "short"]


@dataclass
class Candle:
    open_time: int  # epoch ms, candle open
    open: float
    high: float
    low: float
    close: float


@dataclass
class DayContext:
    """Bookkeeping for a single trading day, per symbol."""
    date_str: str                     # "2026-07-21" (IST calendar date of the anchor candle)
    anchor_high: float
    anchor_low: float
    traded_today: bool = False        # entered a position today already
    stopped_out_today: bool = False   # got stopped out today -> no re-entry today
    position_side: Optional[Side] = None
    current_sl: Optional[float] = None


def apply_buffer(level: float, side: Side, is_sl_for_long: bool, buffer_pct: float) -> float:
    """
    Push the SL further away from price so a wick-touch of the raw candle low/high
    doesn't close the position. For a long, SL sits BELOW the candle low.
    For a short, SL sits ABOVE the candle high.
    """
    factor = buffer_pct / 100.0
    if is_sl_for_long:
        return level * (1 - factor)
    return level * (1 + factor)


def build_day_context(anchor_candle: Candle, date_str: str) -> DayContext:
    return DayContext(
        date_str=date_str,
        anchor_high=anchor_candle.high,
        anchor_low=anchor_candle.low,
    )


def check_breakout(ctx: DayContext, candle: Candle) -> Optional[Side]:
    """
    Given a candle that closed AFTER the anchor candle, on the same trading day,
    determine if it triggers an entry. Returns None if no trigger.
    Only call this if ctx.traded_today is False and ctx.stopped_out_today is False.
    """
    broke_high = candle.close > ctx.anchor_high
    broke_low = candle.close < ctx.anchor_low

    if broke_high and not broke_low:
        return "long"
    if broke_low and not broke_high:
        return "short"
    if broke_high and broke_low:
        # Extremely wide candle that engulfs the range on both sides by wick;
        # impossible for close to be both above high and below low simultaneously,
        # this branch is unreachable in practice but kept for completeness.
        return None
    return None  # close landed back inside the range -> no trigger


def initial_sl(ctx: DayContext, side: Side, anchor_candle: Candle, buffer_pct: float) -> float:
    if side == "long":
        return apply_buffer(ctx.anchor_low, side, True, buffer_pct)
    return apply_buffer(ctx.anchor_high, side, False, buffer_pct)


def ratchet_sl(current_sl: float, side: Side, closed_candle: Candle, buffer_pct: float) -> float:
    """
    Called every time a NEW candle closes while a position is open.
    Moves the SL to the just-closed candle's low (long) / high (short) + buffer,
    but NEVER moves it backwards (a ratchet only tightens, never loosens).
    """
    if side == "long":
        candidate = apply_buffer(closed_candle.low, side, True, buffer_pct)
        return max(current_sl, candidate)
    else:
        candidate = apply_buffer(closed_candle.high, side, False, buffer_pct)
        return min(current_sl, candidate)


def sl_hit(side: Side, sl_price: float, candle: Candle) -> bool:
    """Would this candle's range have triggered the stop (for our own bookkeeping /
    reconciliation; the REAL stop lives as a resting order on the exchange, this is
    only used to keep local state in sync)."""
    if side == "long":
        return candle.low <= sl_price
    return candle.high >= sl_price


def round_quantity(raw_qty: float, step: float) -> float:
    """Round position size down to the exchange's allowed quantity step."""
    if step <= 0:
        return raw_qty
    d = Decimal(str(raw_qty))
    s = Decimal(str(step))
    return float((d // s) * s)
