import logging
import os
import requests
import urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables")

logging.basicConfig(level=logging.INFO)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def get_weather(icao: str) -> str:
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
        r = requests.get(url, headers=HEADERS, timeout=10)
        text = r.text.strip()
        if text:
            return f"Погода {icao}\n\n{text}"
        return f"Погода для {icao} не найдена"
    except Exception as e:
        return f"Ошибка погоды: {str(e)[:100]}"

def get_notam(icao: str) -> str:
    try:
        url = f"https://api.faa.gov/notams?locations={icao}&format=json"
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        data = r.json()
        notams = data.get("notams", [])
        if not notams:
            return f"Активных NOTAM для {icao} нет"
        result = f"NOTAM {icao} ({len(notams)} шт.):\n\n"
        for item in notams[:7]:
            text = item.get("text", "—").replace("\n", " ").strip()
            result += f"• {text[:300]}...\n\n"
        return result.strip()
    except Exception as e:
        return f"NOTAM недоступны: {str(e)[:100]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет!\n"
        "Я выдаю погоду (METAR/TAF) и NOTAM по ICAO-коду аэропорта.\n\n"
        "Примеры:\n"
        "/weather UAAA  → погода для Алматы\n"
        "/notam UAAA    → NOTAM для Алматы\n\n"
        "Или просто напиши ICAO — бот сам выдаст всё сразу!"
    )

async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Укажи ICAO: /weather UAAA")
        return
    icao = context.args[0].upper()
    await update.message.reply_text(get_weather(icao))

async def notam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Укажи ICAO: /notam UAAA")
        return
    icao = context.args[0].upper()
    await update.message.reply_text(get_notam(icao))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip().upper()
    if len(text) == 4 and text.isalpha():
        w = get_weather(text)
        n = get_notam(text)
        await update.message.reply_text(f"{w}\n\n{n}")
    else:
        await update.message.reply_text("Напиши ICAO-код (например UAAA)")

def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("notam", notam))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен в Render!")
    app.run_polling()

if __name__ == "__main__":
    main()
