# Candle-to-Candle

Independent trading bot — separate repo, separate Railway service, separate Telegram bot.
Shares the CoinDCX API key/secret with TradeVerse (account-level key, no conflict since
symbols don't overlap).

## Strategy (confirmed spec — updated 2026-07-22)

- 4H candles, IST. 6 candles/day: 01:30, 05:30, 09:30, 13:30, 17:30, 21:30.
- **Trading days: full week (Mon–Sun)**. Originally Tue–Fri only, back when this ran
  both BTC and GOLD (GOLD trades Mon–Fri only, so Tue–Fri was a shared-calendar
  compromise). GOLD has since been dropped entirely (see below) and BTC trades 24/7,
  so there's no constraint left — full week, including weekends.
- Each day: the **01:30 candle closes** (i.e. the 4H candle built from the 1:30, 2:30,
  3:30, 4:30 hourly bars, closing at 5:30) → its high/low becomes the day's reference range.
- Watch **every later candle that day** for a close beyond the range:
  - close above range-high → **long**
  - close below range-low → **short**
  - a candle wicking through both sides → direction decided by where the **close** lands
- **Initial SL** = opposite extreme of the 01:30 candle, extended **0.5–1% further out**
  (buffer so a wick-touch doesn't stop us out on noise).
- On every subsequent candle close, **ratchet the SL** to that candle's low (long) /
  high (short), same buffer applied. Ratchet only tightens, never loosens. Every
  candle close now logs the check explicitly (candle extreme, current SL, candidate
  SL, tightened-or-not) — not just when it actually changes.
- **No re-entry same day** after a stop-out.
- Trade can run across multiple days — only exits are: trailing SL hit, or manual `/close`.
  (The old mandatory Friday force-close no longer applies now that weekends are
  trading days — there's no "gap" left to protect against.)
- **No fixed take-profit** — the trail is the only automated exit.
- Zero indicators. Pure OHLC.
- **₹10,000 position size, 10x leverage**, INR margin, BTC only.

## Why GOLD was dropped

GOLD (`B-XAUT_USDT`) was tradeable manually through the CoinDCX app, but every API
order-creation attempt failed with `"Instrument is not active"` across 4 separate fix
attempts: toggling INR margin on/off, removing the inline stop-loss from the entry
order, none of it changed the result. Since BTC's identically-shaped requests succeed
and a manual GOLD trade works fine, this points at something account/API-permission or
leverage-tier related on CoinDCX's side for this specific instrument — not something
fixable from this client. Revisit if CoinDCX support clarifies; re-adding it is a
one-line change in `coindcx_client.py`'s `SYMBOL_MAP`.

## Telegram

- Alerts out: new day range, entries, SL ratchets, stop-outs.
- `/status` — current position, entry, SL, P&L.
- `/close BTC` / `/close all` — manual flatten, anytime.
- `/health` — uptime + last poll status, on demand.
- Daily heartbeat (default 08:00 IST) — automatic "still alive" alert.

## Architecture

```
app/
  config.py         All tunables + env vars
  strategy.py        Pure candle logic — no exchange calls, fully unit-testable
  coindcx_client.py  Exchange client — order placement, positions, candles
  state_store.py     Day-bookkeeping persistence (NOT the source of truth for positions)
  telegram_bot.py    Two-way Telegram (alerts + /status + /close)
  main.py            Orchestration loop + startup reconciliation
```

### Why the SL lives on the exchange, not in memory

`coindcx_client.py` is adapted directly from TradeVerse's tested, live client — not
re-derived from docs. Its mechanism is actually better than a resting stop order: the
stop-loss is attached to the entry order itself (`stop_loss_price`), and ratcheting it
later is a single `update_stop_loss()` call against the position — no order id to
track, cancel, or recreate. The bot's local state file only tracks day-bookkeeping
(today's range, whether we've already traded today) — it is **never trusted** for
whether a position actually exists. Every poll cycle (not just at startup) calls
`get_open_positions()` to check live truth; if the bot thinks a position is open but
the exchange shows flat, that's treated as a stop-out automatically. This is the exact
failure mode that bit TradeVerse (in-memory-only state wiped on redeploy) — designed
out here from day one.

## ⚠️ Status of previously-flagged items

1. **INR margin currency** — confirmed working for BTC in production (real entry
   executed, position confirmed INR-margined via CoinDCX app screenshot).
2. **GOLD instrument details** — moot, GOLD dropped (see above).
3. **Candle endpoint `4h` interval** — confirmed CoinDCX does NOT support native `4h`
   (`BFF-SO-004` error). Fixed by fetching `1h` candles and aggregating 4 at a time into
   synthetic 4H candles, bucketed on UTC 4-hour boundaries — verified these align
   exactly with the 01:30/05:30/etc IST candle times this strategy uses.
4. **Supervised dry run** — done; real BTC entry executed and confirmed via CoinDCX app
   (avg entry 66,233.1 USDT, SL 64,736.6 USDT, INR-margined, TP correctly "Not Added").
5. **Sizing bug (since fixed)** — an earlier version converted ₹ to quantity without
   accounting for the USDT/INR rate, inflating position size ~90-100x. Fixed by fetching
   the live rate from CoinDCX's public `/exchange/ticker` (`USDTINR` market) and
   converting properly before computing quantity.
6. **Inline stop-loss incompatibility (since fixed)** — attaching `stop_loss_price`
   directly on the entry order caused `"Instrument is not active"` for GOLD (though not
   BTC). Entry is now two steps: plain market order, then `update_stop_loss()` as a
   separate call — works regardless of whether an instrument supports bracket orders.
7. **SL-attach failure handling** — if the SL-attach step ever fails after a successful
   entry, the bot alerts urgently and retries every poll cycle (not waiting for the next
   candle) until it succeeds, since an unprotected live position is time-sensitive.
8. **Telegram token leaking into logs (since fixed)** — `httpx`'s request logging
   included the full Telegram API URL, which embeds the bot token. Silenced to WARNING
   level so this no longer appears in logs at all.

## Deploy (Railway)

1. New GitHub repo, push this code.
2. New Railway service, connect the repo.
3. **Attach a Railway volume**, mount at `/data` (matches `STATE_DIR` default) — this is
   what makes state survive redeploys.
4. Set environment variables (see `.env.example`): `COINDCX_API_KEY`,
   `COINDCX_API_SECRET` (reused from TradeVerse), new `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`.
5. Start command: `python -m app.main` (already in `Procfile`).
