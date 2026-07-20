"""
Persistence.

Important design decision: this file NEVER holds the "source of truth" for whether a
position or stop-order actually exists. That truth always comes live from CoinDCX.
This store only holds cheap day-bookkeeping (today's anchor range, whether we've
already traded/stopped-out today) so a redeploy doesn't force us to re-derive the
day's context from scratch. On every startup, main.py reconciles this against the
live exchange state before trusting anything here.
"""
import json
import os
import threading
from typing import Dict, Any

from app.config import STATE_FILE, STATE_DIR

_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def load_state() -> Dict[str, Any]:
    _ensure_dir()
    if not os.path.exists(STATE_FILE):
        return {}
    with _lock:
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}


def save_state(state: Dict[str, Any]) -> None:
    _ensure_dir()
    with _lock:
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, STATE_FILE)  # atomic on same filesystem


def get_symbol_state(state: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    return state.get(symbol, {})


def set_symbol_state(state: Dict[str, Any], symbol: str, symbol_state: Dict[str, Any]) -> Dict[str, Any]:
    state[symbol] = symbol_state
    return state
