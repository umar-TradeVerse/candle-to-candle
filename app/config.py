"""
Candle-to-Candle — configuration.
Everything tunable lives here. Secrets come from environment variables (set in Railway).
"""
import os
from zoneinfo import ZoneInfo

# ---------- Exchange ----------
COINDCX_API_KEY = os.environ["COINDCX_API_KEY"]
COINDCX_API_SECRET = os.environ["COINDCX_API_SECRET"]
# Base URLs, SYMBOL_MAP, and margin currency now live in coindcx_client.py
# (kept alongside the client code they belong to, adapted from TradeVerse's tested client).

# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # your chat/user id, alerts go here

# ---------- Risk / sizing ----------
LEVERAGE = 10
POSITION_SIZE_INR = 10000  # flat per symbol for v1 (risk-normalized sizing is a fast-follow)

# SL buffer: how far beyond the candle low/high the stop actually sits,
# so a brief wick-touch doesn't stop us out. Applied at every ratchet step.
SL_BUFFER_PCT = float(os.environ.get("SL_BUFFER_PCT", "0.5"))  # percent

# ---------- Calendar ----------
IST = ZoneInfo("Asia/Kolkata")
# Python weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
# Full week now that GOLD (Mon-Fri only) is dropped and BTC (24/7) is the sole symbol.
TRADING_WEEKDAYS = {0, 1, 2, 3, 4, 5, 6}
TRADES_WEEKENDS = {5, 6}.issubset(TRADING_WEEKDAYS)

CANDLE_INTERVAL = "4h"
CANDLE_INTERVAL_MINUTES = 240

# The daily "anchor" candle opens at 01:30 IST (this is the UTC 00:00 4H boundary,
# displayed in IST). Its high/low becomes the day's reference range.
ANCHOR_CANDLE_HOUR_IST = 1
ANCHOR_CANDLE_MINUTE_IST = 30

# Force-close: flatten any open position before the weekend, no matter the P&L.
# Only applies if TRADES_WEEKENDS is False — if Sat/Sun are trading days, there's no
# "weekend gap" to protect against, so this is skipped entirely (see main.py).
FRIDAY_FORCE_CLOSE_HOUR_IST = 21
FRIDAY_FORCE_CLOSE_MINUTE_IST = 30

# ---------- Persistence ----------
# Point this at a Railway volume mount so state survives redeploys.
# Even so, the bot always reconciles live position/order truth from CoinDCX on startup —
# this file only holds day-bookkeeping (today's range, "already traded today" flags), never
# the source of truth for whether a position/stop actually exists.
STATE_DIR = os.environ.get("STATE_DIR", "/data")
STATE_FILE = os.path.join(STATE_DIR, "candle_to_candle_state.json")

# ---------- Polling ----------
# How often the scheduler wakes up to check "has a new candle closed" / "did price break the range".
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

# ---------- Heartbeat ----------
# A once-a-day "I'm alive" alert so a silent crash overnight doesn't go unnoticed —
# you don't have to remember to check /status yourself.
HEARTBEAT_HOUR_IST = int(os.environ.get("HEARTBEAT_HOUR_IST", "8"))
HEARTBEAT_MINUTE_IST = int(os.environ.get("HEARTBEAT_MINUTE_IST", "0"))

# ---------- Entry retry ----------
# If an entry fails, retry — but not every single poll cycle. This throttles retries
# (and the matching Telegram alert) to once per this many seconds, so a failure doesn't
# spam every 30s. No day-abandonment cap (removed 2026-07-22 per request) — retries are
# indefinite, bounded naturally by the candle itself (a new candle closing supersedes
# the breakout regardless).
ENTRY_RETRY_BACKOFF_SECONDS = int(os.environ.get("ENTRY_RETRY_BACKOFF_SECONDS", "300"))
