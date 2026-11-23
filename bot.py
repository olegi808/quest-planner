"""Минимальный бот для запуска мини-аппа.

pip install aiogram>=3
BOT_TOKEN=... WEBAPP_URL=https://... python bot.py
"""
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]  # обязательно https://

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📅 Открыть планер", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await message.answer(
        "Привет! Это твой личный планер: задачи, привычки, дашборд и статистика.\n"
        "Данные хранятся в твоём облаке Telegram и синхронизируются между устройствами.",
        reply_markup=kb,
    )


async def main() -> None:
    bot = Bot(TOKEN)
    # Кнопка «Планер» слева от поля ввода в чате с ботом
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Планер", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
