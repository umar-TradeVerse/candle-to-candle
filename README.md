# Candle-to-Candle

Independent trading bot — separate repo, separate Railway service, separate Telegram bot.
Shares the CoinDCX API key/secret with TradeVerse (account-level key, no conflict since
symbols don't overlap).

## Strategy (confirmed spec)

- 4H candles, IST. 6 candles/day: 01:30, 05:30, 09:30, 13:30, 17:30, 21:30.
- **Trading days: Tuesday–Friday only**, one calendar for both symbols (BTC trades 24/7,
  GOLD trades Mon–Fri; unifying on Tue–Fri avoids two different candle-count logics
  per symbol — a deliberate simplicity trade-off, not a data-quality workaround).
- Each day: the **01:30 candle closes** → its high/low becomes the day's reference range.
- Watch **every later candle that day** for a close beyond the range:
  - close above range-high → **long**
  - close below range-low → **short**
  - a candle wicking through both sides → direction decided by where the **close** lands
- **Initial SL** = opposite extreme of the 01:30 candle, extended **0.5–1% further out**
  (buffer so a wick-touch doesn't stop us out on noise).
- On every subsequent candle close, **ratchet the SL** to that candle's low (long) /
  high (short), same buffer applied. Ratchet only tightens, never loosens.
- **No re-entry same day** after a stop-out.
- Trade can run across multiple days — only exits are: trailing SL hit, manual
  `/close`, or the mandatory Friday force-close.
- **Force-close before the weekend, always** — flattened at the 21:30 IST Friday candle
  boundary regardless of open P&L.
- **No fixed take-profit** — the trail is the only automated exit.
- Zero indicators. Pure OHLC.
- ₹5,000 per symbol (BTC, GOLD), 5x leverage, INR margin, flat sizing for v1.

## Telegram

- Alerts out: new day range, entries, SL ratchets, stop-outs, force-closes.
- `/status` — current position, entry, SL, P&L per symbol.
- `/close BTC` / `/close GOLD` / `/close all` — manual flatten, anytime.

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

## ⚠️ Flagged for review before going live

Everything below is either carried over as-is from TradeVerse's proven client, or is
new and explicitly called out:

1. **INR margin currency** — TradeVerse's tested client never set
   `margin_currency_short_name` (its USDT pairs just used the account's implicit
   default). Since this bot wants INR margin specifically, it's added explicitly in
   `coindcx_client.py` — this one param is **not** proven by TradeVerse's live usage.
   First dry-run should confirm the position actually opens INR-margined.
2. **GOLD instrument details** — `B-XAU_USDT`'s quantity/price increments haven't been
   fetched/checked live yet (TradeVerse never traded gold). BTC's should behave
   identically to TradeVerse since it's the same pair.
3. **Candle endpoint `4h` interval** — TradeVerse only ever requested `1d` and `15m`;
   worth confirming `4h` returns cleanly for both pairs on first run.
4. **One supervised dry run** before real capital: place a tiny test order, confirm
   `/status` reads the position + ROE back correctly, confirm a SL ratchet actually
   moves the stop on CoinDCX's own UI — before the first unattended overnight run.

## Deploy (Railway)

1. New GitHub repo, push this code.
2. New Railway service, connect the repo.
3. **Attach a Railway volume**, mount at `/data` (matches `STATE_DIR` default) — this is
   what makes state survive redeploys.
4. Set environment variables (see `.env.example`): `COINDCX_API_KEY`,
   `COINDCX_API_SECRET` (reused from TradeVerse), new `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`.
5. Start command: `python -m app.main` (already in `Procfile`).
