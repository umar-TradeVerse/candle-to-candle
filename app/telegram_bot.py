"""
Telegram integration — two-way.
  - send_alert(): push notifications for entries, SL ratchets, stop-outs, force-closes
  - /status: show current open positions + SL + unrealized P&L for both symbols
  - /close BTC | /close GOLD | /close all: manual flatten, anytime

Uses python-telegram-bot (v21+, async).
"""
import logging
from typing import Callable, Awaitable

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("telegram_bot")


class TelegramBot:
    def __init__(self, status_fn: Callable[[], Awaitable[str]],
                 close_fn: Callable[[str], Awaitable[str]]):
        """
        status_fn: async callable -> returns a status string to send back
        close_fn: async callable(symbol: "BTC"|"GOLD"|"all") -> returns a result string
        """
        self.status_fn = status_fn
        self.close_fn = close_fn
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("close", self._handle_close))
        self.app.add_handler(CommandHandler("start", self._handle_start))

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Candle-to-Candle bot online.\nCommands:\n/status\n/close BTC|GOLD|all"
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = await self.status_fn()
        await update.message.reply_text(text)

    async def _handle_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /close BTC | /close GOLD | /close all")
            return
        target = context.args[0].upper()
        if target not in ("BTC", "GOLD", "ALL"):
            await update.message.reply_text("Usage: /close BTC | /close GOLD | /close all")
            return
        result = await self.close_fn("all" if target == "ALL" else target)
        await update.message.reply_text(result)

    async def send_alert(self, text: str):
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        except Exception:
            logger.exception("Failed to send Telegram alert")

    async def start_polling(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
