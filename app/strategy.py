"""
Candle-to-Candle strategy — pure logic, no exchange calls, no indicators.

*** STRATEGY REDESIGNED 2026-07-28 *** — entry logic is now confirmation-based on
15m candles (see detect_touch/check_confirmation/check_setup_invalidated/
check_entry_trigger below), replacing the old "any candle closing beyond the OR
triggers immediate entry" approach. The OR construction and post-entry 4H ratchet
are UNCHANGED — only how we get INTO a trade changed.

New entry rules (confirmed spec):
1. OR (Opening Range) = the 01:30-05:30 IST synthetic 4H candle, built by aggregating
   1h candles (unchanged from before) -> anchor_high / anchor_low.
2. From 05:30 onward, scan 15m candles for a "Candle 1" that touches/breaks the OR
   level on either side (a candle breaching BOTH sides is treated as ambiguous/no
   signal, not resolved by close position — see detect_touch).
3. The very NEXT 15m candle ("Candle 2") must CONFIRM: close beyond the OR level AND
   not touch that level again (no wick back through it). If it fails to confirm,
   discard this attempt and go back to scanning for a fresh Candle 1 later that day
   (does not consume the day's one-trade allowance).
4. Once confirmed, wait for a LATER candle to CLOSE beyond Candle 2's high (long) /
   low (short) — that's the actual entry trigger (not intra-candle/live price).
5. While waiting for that trigger, the setup is invalidated (back to scanning for a
   fresh Candle 1) if any candle closes back beyond the ORIGINAL OR level.
6. Initial SL = Candle 2's low (long) / high (short), extended by SL_BUFFER_PCT.
7. Once in a position, switch to the UNCHANGED 4H ratchet mechanism (ratchet_sl below,
   using the same 5:30/9:30/13:30/17:30/21:30/1:30 synthetic 4H boundaries).
8. Max 1 trade/day. No re-entry same day after a stop-out. Position can run across
   multiple days uninterrupted (no forced close).
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


# ---------- NEW: confirmation-based entry state machine (15m candles) ----------

def detect_touch(anchor_high: float, anchor_low: float, candle: Candle) -> Optional[Side]:
    """
    "Candle 1" detection: does this candle touch/break the OR level on either side?
    A candle breaching BOTH sides (wide/volatile candle) is ambiguous — skip it
    rather than guess, and keep scanning on the next candle.
    """
    touched_high = candle.high > anchor_high
    touched_low = candle.low < anchor_low
    if touched_high and touched_low:
        return None  # ambiguous, safest to skip
    if touched_high:
        return "long"
    if touched_low:
        return "short"
    return None


def check_confirmation(anchor_high: float, anchor_low: float, direction: Side, candle: Candle) -> bool:
    """
    "Candle 2" confirmation check — applies ONLY to the single candle immediately
    following Candle 1. Must CLOSE beyond the OR level AND not touch it again
    (no wick back through it — the entire candle range must stay beyond the level).
    """
    if direction == "long":
        return candle.close > anchor_high and candle.low > anchor_high
    else:
        return candle.close < anchor_low and candle.high < anchor_low


def check_setup_invalidated(anchor_high: float, anchor_low: float, direction: Side, candle: Candle) -> bool:
    """
    While waiting for the entry trigger (after Candle 2 confirmed), the setup is
    invalidated if any candle closes back beyond the ORIGINAL OR level — i.e. price
    gave back the whole confirmed move. Back to scanning for a fresh Candle 1.
    """
    if direction == "long":
        return candle.close < anchor_high
    else:
        return candle.close > anchor_low


def check_entry_trigger(candle2_high: float, candle2_low: float, direction: Side, candle: Candle) -> bool:
    """Actual entry fires when a (later) candle CLOSES beyond Candle 2's high/low —
    not on intra-candle/live price crossing it."""
    if direction == "long":
        return candle.close > candle2_high
    else:
        return candle.close < candle2_low


def initial_sl_from_candle2(direction: Side, candle2_low: float, candle2_high: float, buffer_pct: float) -> float:
    """Initial SL is now based on Candle 2 (the confirmation candle), not the OR
    candle itself — a meaningful change from the old spec."""
    if direction == "long":
        return apply_buffer(candle2_low, direction, True, buffer_pct)
    return apply_buffer(candle2_high, direction, False, buffer_pct)


# ---------- Unchanged: post-entry 4H ratchet ----------

def check_breakout(ctx: DayContext, candle: Candle) -> Optional[Side]:
    """
    *** SUPERSEDED 2026-07-28 by the confirmation state machine above *** — kept here
    for backward compatibility / reference only, no longer called by main.py.
    """
    broke_high = candle.close > ctx.anchor_high
    broke_low = candle.close < ctx.anchor_low

    if broke_high and not broke_low:
        return "long"
    if broke_low and not broke_high:
        return "short"
    if broke_high and broke_low:
        return None
    return None  # close landed back inside the range -> no trigger


def initial_sl(ctx: DayContext, side: Side, anchor_candle: Candle, buffer_pct: float) -> float:
    """*** SUPERSEDED 2026-07-28 *** — kept for reference only, use
    initial_sl_from_candle2() instead. Initial SL is no longer based on the OR
    candle itself."""
    if side == "long":
        return apply_buffer(ctx.anchor_low, side, True, buffer_pct)
    return apply_buffer(ctx.anchor_high, side, False, buffer_pct)


def ratchet_sl(current_sl: float, side: Side, closed_candle: Candle, buffer_pct: float) -> float:
    """
    Called every time a NEW synthetic 4H candle closes while a position is open.
    Moves the SL to the just-closed candle's low (long) / high (short) + buffer,
    but NEVER moves it backwards (a ratchet only tightens, never loosens).
    UNCHANGED by the 2026-07-28 redesign — this only governs post-entry management.
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
