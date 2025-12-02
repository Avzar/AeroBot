import os
import requests
import urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.environ["BOT_TOKEN"]
HEADERS = {"User-Agent": "AeroBot/1.0 (Telegram @your_bot)"}

def get_weather(icao: str) -> str:
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
        r = requests.get(url, headers=HEADERS, timeout=10)
        text = r.text.strip()
        return f"🌤️ Погода {icao.upper()}\n\n{text}" if text else "Погода не найдена 🙄"
    except:
        return "Ошибка получения METAR/TAF"

def get_notam(icao: str) -> str:
    try:
        url = f"https://api.faa.gov/notams?locations={icao}&format=json"
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        data = r.json().get("notams", [])
        if not data:
            return "✅ Активных NOTAM нет"
        result = f"📢 NOTAM {icao.upper()} ({len(data)} шт.):\n\n"
        for n in data[:6]:
            text = n.get("text", "").replace("\n", " ")[:320]
            result += f"• {text}...\n\n"
        return result.strip()
    except:
        return "NOTAM временно недоступны"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✈️ Привет, \n\n"
        "Просто напиши ICAO-код аэропорта — получишь METAR/TAF + NOTAM сразу\n\n"
        "Пример: UAAA"
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if len(text) == 4 and text.isalpha():
        await update.message.reply_text(
            get_weather(text) + "\n\n" + get_notam(text),
            disable_web_page_preview=True
        )

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

print("🚀 Авиабот запущен в облаке!")
app.run_polling()

