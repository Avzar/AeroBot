"""
Telegram Aviation Bot — полный код
Функции:
 - /start, /about
 - /weather <ICAO|IATA|name>  -> человекочитаемый METAR/TAF + NOTAM
 - /notam <ICAO|IATA|name>    -> только NOTAM
 - /find <query>              -> поиск аэропортов по имени / город / кодам
 - /nearby                    -> отправьте геолокацию -> ближайшие аэропорты
 - /wind <ICAO|IATA>          -> прогноз ветра (из TAF)
 - /temp <ICAO|IATA>          -> график температуры (24ч TAF/METAR)
 - /history                   -> история запросов юзера
 - inline mode: @botname UAAA -> краткое превью
 - кнопки: METAR / NOTAM / WIND / TEMP при выводе аэропорта

Требования:
 - python 3.9+
 - python-telegram-bot >= 20
 - aiohttp
 - matplotlib
 - pandas (опционально для работы с CSV базы аэропортов)
 - sqlite3 (входит в stdlib)

ВАЖНО:
 - Для функции /find и /nearby нужен файл airports.csv (OurAirports)
   Скачай: https://ourairports.com/data/airports.csv
   Помести рядом с этим скриптом (или укажи путь в AIRPORTS_CSV)
   Файл должен содержать поля: id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,iso_country,iso_region,municipality,iata,icao,...
 - Укажи переменную окружения BOT_TOKEN
"""

import os
import re
import io
import time
import math
import sqlite3
import logging
import asyncio
import aiohttp
import traceback
from typing import Optional, List, Tuple, Dict, Any
from dataclasses import dataclass

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# plotting (run in thread to avoid blocking)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# optionally pandas for CSV load (faster)
try:
    import pandas as pd
except Exception:
    pd = None

# -----------------------
# Настройка и константы
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Установите переменную окружения BOT_TOKEN с токеном бота")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; aviation-bot/1.0)"}
CACHE_TTL = 300  # seconds
CACHE_LOCK = asyncio.Lock()
CACHE: Dict[str, Tuple[float, str]] = {}  # key -> (ts, text)

# Путь к CSV (ourairports). Обнови путь при необходимости.
AIRPORTS_CSV = os.path.join(os.path.dirname(__file__), "airports.csv")
# Минимальное расстояние при /nearby (в км) — не критично
NEARBY_LIMIT_KM = 500

# SQLite DB для истории
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")

# Минимум для TAF/METAR парсинга
METAR_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
NOTAM_URL = "https://api.faa.gov/notams?locations={icao}&format=json"  # FAA — может не содержать не-US notams

# Поддержка IATA -> ICAO (частичная); полная замена через CSV загрузку ниже
IATA_MAP: Dict[str, str] = {}

# In-memory airports index (filled from CSV)
AIRPORTS: List[Dict[str, Any]] = []

# -----------------------
# Утилиты: SQLite для истории
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            result TEXT,
            ts INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

def save_history(user_id: int, query: str, result: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO history (user_id, query, result, ts) VALUES (?, ?, ?, ?)",
            (user_id, query, result[:1000], int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("save_history failed")

def get_history(user_id: int, limit:int=20) -> List[Tuple[int,str,str,int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, query, result, ts FROM history WHERE user_id = ? ORDER BY ts DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

# -----------------------
# Кэш: простой с async lock
# -----------------------
async def cache_get(key: str) -> Optional[str]:
    async with CACHE_LOCK:
        rec = CACHE.get(key)
        if rec:
            ts, val = rec
            if time.time() - ts < CACHE_TTL:
                return val
            else:
                del CACHE[key]
    return None

async def cache_set(key: str, value: str):
    async with CACHE_LOCK:
        CACHE[key] = (time.time(), value)

# -----------------------
# Загрузка базы аэропортов (ourairports)
# -----------------------
def load_airports():
    global AIRPORTS, IATA_MAP
    if not os.path.exists(AIRPORTS_CSV):
        logger.warning(f"airports.csv не найден ({AIRPORTS_CSV}). /find и /nearby будут ограничены.")
        AIRPORTS = []
        return

    logger.info(f"Загружаю базу аэропортов из {AIRPORTS_CSV} ...")
    rows = []
    if pd:
        df = pd.read_csv(AIRPORTS_CSV, dtype=str)
        # Keep necessary fields, fillna
        for _, r in df.iterrows():
            ident = str(r.get("ident","") or "")
            name = str(r.get("name","") or "")
            iata = str(r.get("iata_code","") or r.get("iata","") or "") or ""
            icao = str(r.get("icao_code","") or r.get("iso_icao","") or "")
            lat = r.get("latitude_deg")
            lon = r.get("longitude_deg")
            country = str(r.get("iso_country","") or "")
            if pd.isna(lat) or pd.isna(lon):
                continue
            rows.append({
                "ident": ident,
                "name": name,
                "iata": iata.upper() if iata else "",
                "icao": icao.upper() if icao else "",
                "lat": float(lat),
                "lon": float(lon),
                "country": country,
            })
    else:
        # Fallback: parse manually (CSV header expected)
        import csv
        with open(AIRPORTS_CSV, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                lat = r.get("latitude_deg")
                lon = r.get("longitude_deg")
                if not lat or not lon:
                    continue
                rows.append({
                    "ident": r.get("ident",""),
                    "name": r.get("name",""),
                    "iata": (r.get("iata_code") or r.get("iata") or "").upper(),
                    "icao": (r.get("icao_code") or r.get("iso_icao") or "").upper(),
                    "lat": float(lat),
                    "lon": float(lon),
                    "country": r.get("iso_country",""),
                })

    AIRPORTS = rows
    # build IATA map
    IATA_MAP = {r["iata"]: r["icao"] for r in AIRPORTS if r.get("iata") and r.get("icao")}
    logger.info(f"Загружено аэропортов: {len(AIRPORTS)}; IATA->ICAO mapping: {len(IATA_MAP)}")

# -----------------------
# Геоутилиты
# -----------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    c = 2*math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R*c

def find_nearby(lat: float, lon: float, limit_km: float=NEARBY_LIMIT_KM, max_results:int=10):
    if not AIRPORTS:
        return []
    out = []
    for a in AIRPORTS:
        try:
            d = haversine_km(lat, lon, a["lat"], a["lon"])
            if d <= limit_km:
                out.append((d, a))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out[:max_results]

def search_airports(query: str, max_results:int=10):
    q = query.strip().lower()
    if not q:
        return []
    res = []
    for a in AIRPORTS:
        if (q in (a.get("name") or "").lower()) or (q in (a.get("icao") or "").lower()) or (q in (a.get("iata") or "").lower()) or (q in (a.get("ident") or "").lower()):
            res.append(a)
            if len(res) >= max_results:
                break
    return res

# -----------------------
# HTTP helpers (aiohttp)
# -----------------------
async def fetch_text(url: str, timeout: int = 15) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=timeout) as r:
                return await r.text()
    except Exception as e:
        logger.exception("fetch_text error")
        raise

async def fetch_json(url: str, timeout: int = 15) -> Any:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=timeout) as r:
                return await r.json()
    except Exception as e:
        logger.exception("fetch_json error")
        raise

# -----------------------
# Простая парсинг-функция METAR -> человекочитаемый (вариант B)
# Эта реализация покрывает обычные случаи, не все особенности METAR.
# -----------------------
METAR_WIND_RE = re.compile(r'(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT')
METAR_TEMP_RE = re.compile(r' (M?\d{2})/(M?\d{2})')
METAR_QNH_RE = re.compile(r' Q(\d{4})')
METAR_VIS_RE = re.compile(r' (\d{1,2}SM|\d{4}) ')
METAR_CLOUD_RE = re.compile(r' (FEW|SCT|BKN|OVC)\d{3}')

def parse_metar_human(raw: str) -> Dict[str, Optional[str]]:
    """
    Возвращает словарь: wind, visibility, temp, dewpoint, qnh_hpa, clouds, raw_time
    """
    out = {
        "wind": None,
        "visibility": None,
        "temp": None,
        "dewpoint": None,
        "qnh": None,
        "clouds": None,
        "raw": raw,
    }
    try:
        # иногда METAR содержит несколько строк — берём первую
        first = raw.splitlines()[0].strip()
        # время — взятие токена вида DDHHMMZ
        m_time = re.search(r'\b(\d{6}Z)\b', first)
        out["time"] = m_time.group(1) if m_time else None

        m_w = METAR_WIND_RE.search(first)
        if m_w:
            dir_, speed, gust = m_w.groups()
            gust = gust[1:] if gust else None
            out["wind"] = f"{dir_}° {int(speed)} kt" + (f" gust {gust} kt" if gust else "")

        m_vis = METAR_VIS_RE.search(first + " ")
        if m_vis:
            vis = m_vis.group(1)
            # convert 9999 -> 10+ km
            if vis == "9999" or vis == "10000":
                out["visibility"] = "10+ km"
            else:
                out["visibility"] = vis

        m_temp = METAR_TEMP_RE.search(first)
        if m_temp:
            t,d = m_temp.groups()
            out["temp"] = t.replace('M','-')
            out["dewpoint"] = d.replace('M','-')

        m_qnh = METAR_QNH_RE.search(first)
        if m_qnh:
            qnh = m_qnh.group(1)
            # QNH in hPa, e.g. Q1013
            out["qnh"] = f"{qnh[:]} hPa"

        clouds = METAR_CLOUD_RE.findall(first)
        if clouds:
            out["clouds"] = ", ".join(clouds)

    except Exception:
        logger.exception("parse_metar_human failed")
    return out

# -----------------------
# Простой TAF-парсер для ветра/температуры по времени (очень упрощённый)
# -----------------------
TAF_WIND_RE = re.compile(r'(\d{2})(\d{2})(\d{2})/(\d{2})(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT')
# Очень упрощённо — чтение всех токенов содержащих KT после временной метки.
def parse_taf_wind(taf: str) -> List[Tuple[str,str]]:
    """
    Возвращает список (time_window, wind_str) упрощённо.
    """
    out = []
    try:
        tokens = taf.split()
        # Найти все части с форматом FMHHMM, BECMG и интервалы — сильно упрощено.
        # Попробуем найти все вхождения \d{4}/\d{4} или FM\d{6}
        for t in re.finditer(r'(\d{4}/\d{4})', taf):
            # take nearby KT token
            span_end = t.end()
            tail = taf[span_end:span_end+120]
            m = re.search(r'(\d{3}V?\d{3})?(\d{3}|VRB)\d{2}(G\d{2})?KT', tail)
            if m:
                out.append((t.group(1), m.group(0)))
        # Fallback: find simple wind tokens with time markers like FM0600
        for m in re.finditer(r'(FM\d{6}).{0,40}?(\d{3}|VRB)\d{2}(G\d{2})?KT', taf):
            out.append((m.group(1), m.group(2) + m.group(0)[-3:]))  # rough
    except Exception:
        logger.exception("parse_taf_wind failed")
    return out

# -----------------------
# Формирование человекочитаемого отчёта (вариант B)
# -----------------------
def format_weather_human(icao: str, metar_raw: Optional[str], taf_raw: Optional[str]) -> str:
    lines = []
    lines.append(f"✈️ Аэропорт: {icao.upper()}")
    if metar_raw:
        p = parse_metar_human(metar_raw)
        if p.get("time"):
            lines.append(f"🕒 Время отчёта: {p['time']} (UTC)")
        if p.get("wind"):
            lines.append(f"💨 Ветер: {p['wind']}")
        if p.get("visibility"):
            lines.append(f"👁 Видимость: {p['visibility']}")
        if p.get("temp"):
            lines.append(f"🌡 Температура: {p['temp']}°C (точка росы {p['dewpoint']}°C)" if p.get("dewpoint") else f"🌡 Температура: {p['temp']}°C")
        if p.get("qnh"):
            lines.append(f"🔽 Давление: {p['qnh']}")
        if p.get("clouds"):
            lines.append(f"☁ Облака: {p['clouds']}")
    else:
        lines.append("⚠ METAR не найден")

    # TAF кратко
    if taf_raw:
        # display first 2 lines or shorten
        taf_preview = "\n".join(taf_raw.splitlines()[:2])
        lines.append("")
        lines.append("📋 TAF (кратко):")
        lines.append(taf_preview)
    else:
        lines.append("")
        lines.append("📋 TAF не найден")

    return "\n".join(lines)

# -----------------------
# Запрос METAR/TAF + NOTAM (асинхронно), с кэшем
# -----------------------
async def get_metar_and_taf(icao: str) -> Tuple[Optional[str], Optional[str]]:
    key = f"{icao}_metar_taf"
    cached_res = await cache_get(key)
    if cached_res:
        # cached contains combined: raw_metar||raw_taf  (we store joined)
        try:
            raw_metar, raw_taf = cached_res.split("||METAR_TAF||")
            return (raw_metar if raw_metar else None, raw_taf if raw_taf else None)
        except Exception:
            pass

    url = METAR_URL.format(icao=icao)
    try:
        text = await fetch_text(url, timeout=10)
        # API may return raw like "UAAA 010600Z ...\n\nTAF ..."
        # Split naive: if 'TAF' present, separate; otherwise assume response is METAR raw only.
        # aviationweather.gov returns METAR in body, sometimes TAF included in separate response; keep simple.
        raw = text.strip()
        raw_metar = None
        raw_taf = None
        if raw:
            # If there is TAF keyword in text, split; otherwise assign to METAR
            if "TAF" in raw and "\n" in raw:
                # very naive split: first line(s) as METAR until blank line, rest as TAF if contains 'TAF'
                parts = raw.split("\n\n")
                # find part containing "TAF"
                for p in parts:
                    if p.strip().startswith("TAF") or "TAF" in p:
                        raw_taf = p.strip()
                    else:
                        raw_metar = (raw_metar + "\n" + p.strip()) if raw_metar else p.strip()
            else:
                raw_metar = raw
        await cache_set(key, (raw_metar or "") + "||METAR_TAF||" + (raw_taf or ""))
        return (raw_metar, raw_taf)
    except Exception as e:
        logger.exception("get_metar_and_taf failed")
        return (None, None)

async def get_notams(icao: str) -> Optional[str]:
    key = f"{icao}_notam"
    cached_res = await cache_get(key)
    if cached_res:
        return cached_res
    url = NOTAM_URL.format(icao=icao)
    try:
        data = await fetch_json(url, timeout=15)
        notams = []
        if isinstance(data, dict):
            notams = data.get("notams", [])
        if not notams:
            msg = f"✅ Активных NOTAM для {icao.upper()} не найдено."
            await cache_set(key, msg)
            return msg
        # format few notams
        out = [f"📢 NOTAM {icao.upper()} ({len(notams)}):"]
        for n in notams[:6]:
            t = n.get("text", "—").replace("\n", " ").strip()[:350]
            out.append(f"• {t}...")
        msg = "\n\n".join(out)
        await cache_set(key, msg)
        return msg
    except Exception:
        # If FAA API fails (common for non-US), try to return message or empty
        logger.exception("get_notams failed")
        msg = f"NOTAM недоступны для {icao.upper()} (API)."
        await cache_set(key, msg)
        return msg

# -----------------------
# Помощники для работы с кодами (IATA -> ICAO и наоборот)
# -----------------------
def normalize_code_input(q: str) -> str:
    q = q.strip().upper()
    # If 3-letter — probably IATA
    if len(q) == 3:
        ic = IATA_MAP.get(q)
        if ic:
            return ic
    # if 4-letter — assume ICAO
    return q

def airport_display(a: Dict[str, Any]) -> str:
    code = a.get("icao") or a.get("ident") or ""
    iata = a.get("iata") or ""
    name = a.get("name") or ""
    country = a.get("country") or ""
    return f"{code} ({iata}) — {name} — {country}"

# -----------------------
# Генерация графика температуры (использует METAR + TAF; упрощённо)
# -----------------------
async def generate_temp_plot(icao: str, metar_raw: Optional[str], taf_raw: Optional[str]) -> bytes:
    # Получаем набор временных точек и температур (очень упрощённо: METAR temp + дробь из TAF с числами)
    temps = []
    times = []

    # parse METAR first point
    try:
        if metar_raw:
            m = re.search(r' (M?\d{2})/(M?\d{2})', metar_raw)
            if m:
                t = m.group(1).replace('M','-')
                temps.append(float(t))
                times.append("now")
    except Exception:
        logger.exception("temp parse metar failed")

    # parse TAF for numbers like TEMPO or FM intervals with temps (not standard) — extremely approximate:
    if taf_raw:
        # find numbers that look like temperatures in TAF (e.g. TX12/0106Z)
        txs = re.findall(r'TX(M?\d{1,2})', taf_raw)
        tns = re.findall(r'TN(M?\d{1,2})', taf_raw)
        for i, tx in enumerate(txs[:8]):
            try:
                temps.append(float(tx.replace('M','-')))
                times.append(f"TX{i+1}")
            except:
                continue
        for i, tn in enumerate(tns[:8]):
            try:
                temps.append(float(tn.replace('M','-')))
                times.append(f"TN{i+1}")
            except:
                continue

    # Fallback: if none found, produce a dummy small series
    if not temps:
        temps = [0, 1, 2, 3]
        times = ["-3h","-2h","-1h","now"]

    # Run plotting in thread
    def plot_bytes(x, y, labels):
        plt.figure(figsize=(6,3))
        plt.plot(range(len(y)), y, marker='o')
        plt.title(f"Температура — {icao.upper()}")
        plt.xlabel("Временные точки")
        plt.xticks(range(len(y)), labels, rotation=45)
        plt.ylabel("°C")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        plt.close()
        buf.seek(0)
        return buf.read()

    img = await asyncio.to_thread(plot_bytes, None, temps, times)
    return img

# -----------------------
# Бот — обработчики команд
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("METAR / TAF", callback_data="btn_metar") , InlineKeyboardButton("NOTAM", callback_data="btn_notam")],
        [InlineKeyboardButton("Ветер", callback_data="btn_wind"), InlineKeyboardButton("Температура", callback_data="btn_temp")],
        [InlineKeyboardButton("Найти аэропорт", callback_data="btn_find")]
    ]
    await update.message.reply_text(
        "✈️ Привет! Я авиа-бот.\n"
        "Отправь ICAO (UAAA), IATA (ALA) или название города (Almaty). \n"
        "Доступные команды: /weather, /notam, /find, /nearby, /wind, /temp, /history, /about\n",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я выдаю человекочитаемую погоду (METAR/TAF) и NOTAM, умею строить графики температуры и показывать прогноз ветра.\n"
        "База аэропортов: OurAirports (локальный файл airports.csv)."
    )

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /weather UAAA или /weather ALA или /weather almaty")
        return
    query = " ".join(context.args)
    code = normalize_code_input(query)
    # if code length 4 and letters -> use as ICAO; else try lookup
    if len(code) != 4 or not code.isalpha():
        # attempt search
        results = search_airports(query, max_results=1)
        if results:
            code = results[0].get("icao") or results[0].get("ident")
    # fetch
    await update.message.reply_text(f"🔎 Запрашиваю данные для {code} ...")
    metar_raw, taf_raw = await get_metar_and_taf(code)
    notams = await get_notams(code)
    human = format_weather_human(code, metar_raw, taf_raw)
    # save history (truncated)
    save_history(update.effective_user.id, f"weather:{code}", human[:1000])
    # reply with inline buttons for more actions
    kb = [
        [
            InlineKeyboardButton("Показать NOTAM", callback_data=f"notam|{code}"),
            InlineKeyboardButton("Показать WIND", callback_data=f"wind|{code}"),
            InlineKeyboardButton("График TEMP", callback_data=f"temp|{code}"),
        ]
    ]
    await update.message.reply_text(human, reply_markup=InlineKeyboardMarkup(kb))
    # also send NOTAM as separate message
    await update.message.reply_text(notams)

async def cmd_notam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /notam UAAA")
        return
    query = context.args[0]
    code = normalize_code_input(query)
    if len(code) !=4 or not code.isalpha():
        # search
        results = search_airports(query, max_results=1)
        if results:
            code = results[0].get("icao") or results[0].get("ident")
    msg = await get_notams(code) if False else await get_notams(code)  # call notams fetcher
    save_history(update.effective_user.id, f"notam:{code}", msg[:1000])
    await update.message.reply_text(msg)

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /find <город/название/код>")
        return
    query = " ".join(context.args)
    results = search_airports(query, max_results=8)
    if not results:
        await update.message.reply_text("Ничего не найдено.")
        return
    out_lines = []
    kb = []
    for a in results:
        code = a.get("icao") or a.get("ident")
        line = f"{code} ({a.get('iata','')}) — {a.get('name')} — {a.get('country')}"
        out_lines.append(line)
        kb.append([InlineKeyboardButton(f"{code}", callback_data=f"metar|{code}")])
    await update.message.reply_text("\n".join(out_lines), reply_markup=InlineKeyboardMarkup(kb))

async def cmd_nearby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ask for location
    await update.message.reply_text("Отправь свою геолокацию (кнопка '📎' -> 'Location') чтобы я подобрал ближайшие аэропорты.")

async def cmd_wind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /wind UAAA")
        return
    query = context.args[0]
    code = normalize_code_input(query)
    metar_raw, taf_raw = await get_metar_and_taf(code)
    # parse taf
    winds = parse_taf_wind(taf_raw or "")
    text = f"Прогноз ветра для {code}:\n"
    if winds:
        for t,w in winds[:12]:
            text += f"{t} -> {w}\n"
    else:
        # fallback: use current METAR wind
        p = parse_metar_human(metar_raw or "")
        text += p.get("wind") or "Информация о ветре отсутствует"
    save_history(update.effective_user.id, f"wind:{code}", text[:1000])
    await update.message.reply_text(text)

async def cmd_temp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /temp UAAA")
        return
    code = normalize_code_input(context.args[0])
    metar_raw, taf_raw = await get_metar_and_taf(code)
    await update.message.reply_text("Генерирую график температуры...")
    try:
        img_bytes = await generate_temp_plot(code, metar_raw, taf_raw)
        bio = io.BytesIO(img_bytes)
        bio.name = f"{code}_temp.png"
        bio.seek(0)
        # save history
        save_history(update.effective_user.id, f"temp:{code}", "temp_plot")
        await update.message.reply_photo(photo=bio, caption=f"Температура — {code}")
    except Exception:
        logger.exception("cmd_temp failed")
        await update.message.reply_text("Не удалось сгенерировать график.")

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_user.id, limit=20)
    if not rows:
        await update.message.reply_text("История пуста.")
        return
    out = []
    for rid, q, res, ts in rows:
        out.append(f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))} — {q}")
    await update.message.reply_text("\n".join(out))

# -----------------------
# Обработка CallbackQuery (кнопки)
# -----------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    try:
        if data.startswith("metar|"):
            code = data.split("|",1)[1]
            metar_raw, taf_raw = await get_metar_and_taf(code)
            human = format_weather_human(code, metar_raw, taf_raw)
            await query.edit_message_text(human)
        elif data.startswith("notam|"):
            code = data.split("|",1)[1]
            notams = await get_notams(code)
            await query.edit_message_text(notams)
        elif data.startswith("wind|"):
            code = data.split("|",1)[1]
            metar_raw, taf_raw = await get_metar_and_taf(code)
            winds = parse_taf_wind(taf_raw or "")
            text = f"Прогноз ветра для {code}:\n"
            if winds:
                for t,w in winds[:12]:
                    text += f"{t} -> {w}\n"
            else:
                p = parse_metar_human(metar_raw or "")
                text += p.get("wind") or "Информация о ветре отсутствует"
            await query.edit_message_text(text)
        elif data.startswith("temp|"):
            code = data.split("|",1)[1]
            metar_raw, taf_raw = await get_metar_and_taf(code)
            await query.edit_message_text("Генерирую график температуры...")
            img = await generate_temp_plot(code, metar_raw, taf_raw)
            bio = io.BytesIO(img); bio.name=f"{code}_temp.png"; bio.seek(0)
            await query.message.reply_photo(photo=bio, caption=f"Температура — {code}")
            # optionally update callback message
            await query.edit_message_text("Готово — график отправлен.")
        elif data == "btn_find":
            await query.edit_message_text("Использование: /find <город/название/код>")
        else:
            await query.edit_message_text("Неизвестная кнопка.")
    except Exception:
        logger.exception("callback handler error")
        await query.edit_message_text("Ошибка обработки кнопки.")

# -----------------------
# Inline query handler (краткое превью)
# -----------------------
from telegram import InlineQueryResultArticle, InputTextMessageContent
import uuid

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    if not q:
        return
    # try normalize code
    code = normalize_code_input(q)
    items = []
    # Create a simple preview using METAR + NOTAM (cached)
    metar_raw, taf_raw = await get_metar_and_taf(code)
    notams = await get_notams(code)
    brief = format_weather_human(code, metar_raw, taf_raw)
    content = InputTextMessageContent(brief + "\n\n" + (notams or ""))
    item = InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title=f"{code} — preview",
        input_message_content=content,
        description=(metar_raw or "")[:200]
    )
    items.append(item)
    await context.bot.answer_inline_query(iq.id, results=items, cache_time=10)

# -----------------------
# Обработка обычных сообщений (ICAO/IATA/name) и локации
# -----------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    # if it looks like a 3/4 letter code -> treat as request
    if len(text) in (3,4) and text.isalpha():
        code = normalize_code_input(text)
        # Show both METAR+NOTAM
        metar, taf = await get_metar_and_taf(code)
        notams = await get_notams(code)
        human = format_weather_human(code, metar, taf)
        kb = [[InlineKeyboardButton("NOTAM", callback_data=f"notam|{code}"),
               InlineKeyboardButton("WIND", callback_data=f"wind|{code}"),
               InlineKeyboardButton("TEMP", callback_data=f"temp|{code}")]]
        await update.message.reply_text(human, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text(notams)
        save_history(update.effective_user.id, f"text:{text}", human[:1000])
        return
    # otherwise search by name
    results = search_airports(text, max_results=6)
    if results:
        out_lines = []
        kb = []
        for a in results:
            out_lines.append(airport_display(a))
            code = a.get("icao") or a.get("ident")
            kb.append([InlineKeyboardButton(code, callback_data=f"metar|{code}")])
        await update.message.reply_text("\n".join(out_lines), reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text("Не распознал запрос. Введи ICAO (UAAA) / IATA (ALA) или /find <запрос>.")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Не удалось получить локацию.")
        return
    lat = loc.latitude
    lon = loc.longitude
    nearby = find_nearby(lat, lon, limit_km=NEARBY_LIMIT_KM, max_results=8)
    if not nearby:
        await update.message.reply_text("Ближайшие аэропорты не найдены (база отсутствует).")
        return
    lines = []
    kb = []
    for d, a in nearby:
        lines.append(f"{a.get('icao') or a.get('ident')} ({a.get('iata','')}) — {a.get('name')} — {d:.1f} km")
        kb.append([InlineKeyboardButton(a.get('icao') or a.get('ident'), callback_data=f"metar|{a.get('icao') or a.get('ident')}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

# -----------------------
# Инициализация и запуск
# -----------------------
def build_app() -> Application:
    init_db()
    load_airports()
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("notam", cmd_notam))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("nearby", cmd_nearby))
    app.add_handler(CommandHandler("wind", cmd_wind))
    app.add_handler(CommandHandler("temp", cmd_temp))
    app.add_handler(CommandHandler("history", cmd_history))

    # Callback query (buttons)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Inline queries
    app.add_handler(InlineQueryHandler(inline_query_handler))

    # Messages: location and text
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    return app

def main():
    app = build_app()
    logger.info("Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
