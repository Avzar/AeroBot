import logging
import requests
import urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Обязательные заголовки — без них API не отвечают
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*"
}

def get_weather(icao: str) -> str:
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
        r = requests.get(url, headers=HEADERS, timeout=12)
        if "METAR" not in r.text and "TAF" not in r.text:
            return f"Погода для {icao} не найдена"
        return f"Погода {icao}\n\n{r.text.strip()}"
    except:
        return f"Ошибка получения погоды для {icao}"

def get_notam(icao: str) -> str:
    try:
        url = f"https://api.faa.gov/notams?locations={icao}&format=json"
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        data = r.json()
        notams = data.get("notams", [])
        if not notams:
            return f"Активных NOTAM для {icao} нет"
        result = f"NOTAM {icao} ({len(notams)} шт.):\n\n"
        for item in notams[:8]:
            text = item.get("text", "—").replace("\n", " ")
            result += f"• {text[:350]}...\n\n"
        return result.strip()
    except Exception as e:
        return "NOTAM сейчас недоступны"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n"
        "Я выдаю погоду (METAR/TAF) и NOTAM по ICAO-коду аэропорта.\n\n"
        "Примеры:\n"
        "/weather UAAA\n"
        "/notam UAAA\n\n"
        "Или просто напиши ICAO — получишь всё сразу!"
    )

async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи ICAO: /weather UAAA")
        return
    await update.message.reply_text(get_weather(context.args[0].upper()))

async def notam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи ICAO: /notam UAAA")
        return
    await update.message.reply_text(get_notam(context.args[0].upper()))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if len(text) == 4 and text.isalpha():
        await update.message.reply_text(get_weather(text) + "\n\n" + get_notam(text))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("notam", notam))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Бот запущен и полностью работает!")
    app.run_polling()

if __name__ == "__main__":
    main()