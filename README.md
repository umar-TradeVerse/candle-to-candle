# Candle-to-Candle

Independent trading bot — separate repo, separate Railway service, separate Telegram bot.
Shares the CoinDCX API key/secret with TradeVerse (account-level key, no conflict since
symbols don't overlap).

## Strategy (confirmed spec — REDESIGNED 2026-07-28)

**Entry logic changed significantly. Post-entry management did not.**

### Opening Range (unchanged)
- 4H candles, IST. The **01:30–05:30 IST candle** (built from the 1:30/2:30/3:30/4:30
  hourly bars) marks the day's **Opening Range** — its high/low.
- **Trading days: full week (Mon–Sun)** — no calendar constraint since GOLD (Mon-Fri
  only) was dropped and BTC trades 24/7.

### Entry — confirmation-based, on 15m candles (NEW)
The old "any candle closing beyond the OR triggers immediate entry" is gone. Entry now
requires a two-candle confirmation on the 15m timeframe:

1. **Candle 1** touches/breaks the OR high or low (a candle piercing both sides is
   ambiguous — skipped, not resolved by guessing).
2. **Candle 2** (the very next 15m candle) must **CLOSE beyond** that OR level **and
   not touch it again** (no wick back through) — this is "confirmation"/"acceptance".
   If Candle 2 fails this (retests the level, or doesn't close beyond it), the attempt
   is discarded and the bot goes back to scanning for a fresh Candle 1 later the same
   day — this does **not** consume the day's one-trade allowance.
3. Once confirmed, the bot waits for a **later candle to CLOSE beyond Candle 2's
   high** (long) / **low** (short) — that's the actual entry trigger. Live/intra-candle
   price crossing it does NOT count; a close is required, same as the OR confirmation.
4. While waiting for that trigger, the setup is **invalidated** if any candle closes
   back beyond the *original* OR level — back to scanning for a fresh Candle 1.
5. **Initial SL = Candle 2's low (long) / high (short)** — extended 0.5–1% further out
   (buffer against wick stop-outs). This is a change from before: SL used to be based
   on the OR candle itself; now it's based on the confirmation candle, which is
   typically much tighter.
6. **Max 1 trade per day.** No re-entry same day after a stop-out.

### Post-entry management (UNCHANGED)
- Once in a position, switch to **4H synthetic candles** (5:30–9:30, 9:30–13:30,
  13:30–17:30, 17:30–21:30, 21:30–1:30).
- On every 4H close, **ratchet the SL** to that candle's low (long) / high (short),
  same buffer. Ratchet only tightens, never loosens.
- **No fixed take-profit** — the trail is the only automated exit.
- Position can run across multiple days uninterrupted — no forced close.
- Zero indicators anywhere. Pure OHLC.
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

## ⚠️ IMPORTANT: leverage must be set manually, one time, before running the bot

CoinDCX ties leverage to a persistent per-pair "position card", not to individual
orders — confirmed via their own docs: *"The leverage displayed on your position card
represents the leverage that will be applied to all futures orders and the position."*
Adjusting it is an add/remove-margin flow in their app, not a simple API value you can
set. Two guessed API endpoints for this both failed (one didn't exist, `404`; the other
was rejected outright) — rather than keep guessing against real money, this client
does **not** attempt to set leverage at all.

**Before running the bot (or whenever you change `LEVERAGE` in `config.py`):**
1. Open the CoinDCX app → B-BTC_USDT futures screen
2. Use the **"Adjust Leverage"** button to set it to match `config.LEVERAGE` exactly
3. Only then will the bot's orders (which send that same leverage inline) succeed —
   otherwise every entry fails with `"Order leverage must be equal to position leverage"`

## Status of previously-flagged items

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
