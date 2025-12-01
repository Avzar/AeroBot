import logging
import os
import requests
import urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

urllib3.disable_warnings()

TOKEN = os.environ["BOT_TOKEN"]

HEADERS = {"User-Agent": "AeroBot/1.0 (+https://t.me/твой_бот)"}

def get_weather(icao):
    try:
        r = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true", 
                        headers=HEADERS, timeout=10)
        return f"Погода {icao}\n\n{r.text.strip()}" if r.text.strip() else "Погода не найдена"
    except:
        return "Ошибка погоды"

def get_notam(icao):
    try:
        r = requests.get(f"https://api.faa.gov/notams?locations={icao}&format=json",
                        headers=HEADERS, timeout=12, verify=False)
        data = r.json()
        notams = data.get("notams", [])
        if not notams:
            return "Активных NOTAM нет"
        res = f"NOTAM {icao} ({len(notams)} шт.):\n\n"
        for n in notams[:7]:
            res += "• " + n.get("text","").replace("\n"," ")[:350] + "...\n\n"
        return res
    except:
        return "NOTAM временно недоступны"

async def start(update: Update, context):
    await update.message.reply_text(
        "Привет!\nЯ выдаю METAR/TAF и NOTAMnПросто напиши ICAO (UAAA, EGLL, OMDB и т.д.)"
    )

async def handle(update: Update, context):
    text = update.message.text.strip().upper()
    if len(text)==4 and text.isalpha():
        await update.message.reply_text(get_weather(text) + "\n\n" + get_notam(text))

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

print("Бот запущен в облаке Render")
app.run_polling()

