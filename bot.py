import logging
import os
import requests
import urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Токен берём из переменной окружения Render
TOKEN = os.environ["BOT_TOKEN"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def get_weather(icao: str) -> str:
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
        r = requests.get(url, headers=HEADERS, timeout=12)
        return f"Погода {icao}\n\n{r.text.strip()}" if r.text.strip() else f"Погода для {icao} не найдена"
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
    except:
        return "NOTAM временно недоступны"

async def start(update: Update, context):
    await update.message.reply_text(
        "Привет!\nЯ выдаю погоду (METAR/TAF) и NOTAM по ICAO-коду.\n\n"
        "Просто напиши код аэропорта — получишь всё сразу!\n"
        "Примеры: UAAA, UUWW, EGLL, OMDB"
    )

async def handle_message(update: Update, context):
    text = update.message.text.strip().upper()
    if len(text) == 4 and text.isalpha():
        await update.message.reply_text(get_weather(text) + "\n\n" + get_notam(text))

def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Бот запущен в облаке!")
    app.run_polling()

if __name__ == "__main__":
    main()
