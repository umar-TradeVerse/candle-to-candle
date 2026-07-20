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
LEVERAGE = 5
POSITION_SIZE_INR = 5000  # flat per symbol for v1 (risk-normalized sizing is a fast-follow)

# SL buffer: how far beyond the candle low/high the stop actually sits,
# so a brief wick-touch doesn't stop us out. Applied at every ratchet step.
SL_BUFFER_PCT = float(os.environ.get("SL_BUFFER_PCT", "0.5"))  # percent

# ---------- Calendar ----------
IST = ZoneInfo("Asia/Kolkata")
TRADING_WEEKDAYS = {1, 2, 3, 4}  # Python weekday(): Mon=0 ... Tue=1, Wed=2, Thu=3, Fri=4

CANDLE_INTERVAL = "4h"
CANDLE_INTERVAL_MINUTES = 240

# The daily "anchor" candle opens at 01:30 IST (this is the UTC 00:00 4H boundary,
# displayed in IST). Its high/low becomes the day's reference range.
ANCHOR_CANDLE_HOUR_IST = 1
ANCHOR_CANDLE_MINUTE_IST = 30

# Force-close: flatten any open position before the weekend, no matter the P&L.
# We flatten at the close of Friday's last 4H candle (21:30 IST candle, closing 01:30 Sat).
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
