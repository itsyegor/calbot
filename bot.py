import os
import re
import json
import html
import time
import base64
import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone, timedelta, date
from email.utils import parseaddr
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
FOLLOWUP_WINDOW_SECONDS = 120
MSK = ZoneInfo("Europe/Moscow")

MAIN_MODEL = "claude-sonnet-5"
TRIAGE_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 3000

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Почта: опрос раз в N минут. 0 — почта выключена.
GMAIL_POLL_MINUTES = int(os.environ.get("GMAIL_POLL_MINUTES", "10"))
GMAIL_QUERY = "newer_than:2d -in:chats -in:drafts"
GMAIL_MAX_ATTEMPTS = 3
GMAIL_MAX_PDF_BYTES = 8 * 1024 * 1024

# Карточки хранятся в SQLite на диске Railway (volume), чтобы переживать перезапуски.
DATA_DIR = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "calbot.db")

TG_TEXT_LIMIT = 3800
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def is_owner(update):
    if update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("Этот бот личный.")
        return False
    return True


# ─── Storage ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- proposal | cancel_offer
    source      TEXT NOT NULL,      -- user | gmail
    status      TEXT NOT NULL,      -- pending | adding | added | cancelled | superseded | deleted | kept
    events      TEXT,               -- JSON: предложенные события
    header      TEXT,               -- HTML-строка над карточкой (для писем)
    view        TEXT,               -- текущий HTML-текст сообщения
    target_ids  TEXT,               -- JSON: created_events.id, которые можно удалить из этого сообщения
    created_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS created_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id     TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    data            TEXT NOT NULL,  -- JSON: событие, как его предложил Claude
    calendar_label  TEXT,
    link            TEXT,
    status          TEXT NOT NULL,  -- active | deleted
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gmail_seen (
    msg_id    TEXT PRIMARY KEY,
    result    TEXT,
    attempts  INTEGER NOT NULL DEFAULT 0,
    seen_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

db = None


def init_db(path=None):
    global db
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    if not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        logger.warning("Volume не подключен: карточки пропадут при следующем деплое. База: %s", path)
    return db


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def meta_get(key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key, value):
    if value is None:
        db.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
    db.commit()


def _card(row):
    if row is None:
        return None
    d = dict(row)
    d["events"] = json.loads(d["events"]) if d["events"] else []
    d["target_ids"] = json.loads(d["target_ids"]) if d["target_ids"] else []
    return d


def save_card(chat_id, message_id, kind, source, status, events=None, header=None, view=None, target_ids=None):
    db.execute(
        "INSERT OR REPLACE INTO cards(chat_id, message_id, kind, source, status, events, header, view, target_ids, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chat_id, message_id, kind, source, status, json.dumps(events or [], ensure_ascii=False),
         header, view, json.dumps(target_ids or []), _iso()),
    )
    db.commit()


def get_card(chat_id, message_id):
    return _card(db.execute("SELECT * FROM cards WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone())


def update_card(chat_id, message_id, **fields):
    for k in ("events", "target_ids"):
        if k in fields:
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE cards SET {cols} WHERE chat_id=? AND message_id=?", (*fields.values(), chat_id, message_id))
    db.commit()


def latest_user_card(chat_id):
    return _card(db.execute(
        "SELECT * FROM cards WHERE chat_id=? AND kind='proposal' AND source='user' "
        "ORDER BY created_at DESC, message_id DESC LIMIT 1", (chat_id,)).fetchone())


def card_age_seconds(card):
    return (_now() - datetime.fromisoformat(card["created_at"])).total_seconds()


def pending_proposals(chat_id):
    rows = db.execute("SELECT * FROM cards WHERE chat_id=? AND kind='proposal' AND status='pending'", (chat_id,)).fetchall()
    return [_card(r) for r in rows]


def has_pending_cancel_offer(created_event_id):
    rows = db.execute("SELECT target_ids FROM cards WHERE kind='cancel_offer' AND status='pending'").fetchall()
    return any(created_event_id in json.loads(r["target_ids"] or "[]") for r in rows)


def add_created_event(calendar_id, event_id, event, calendar_label, link):
    cur = db.execute(
        "INSERT INTO created_events(calendar_id, event_id, data, calendar_label, link, status, created_at) "
        "VALUES (?,?,?,?,?,'active',?)",
        (calendar_id, event_id, json.dumps(event, ensure_ascii=False), calendar_label, link, _iso()),
    )
    db.commit()
    return cur.lastrowid


def get_created_events(ids, only_active=False):
    if not ids:
        return []
    rows = db.execute(f"SELECT * FROM created_events WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        if only_active and d["status"] != "active":
            continue
        out.append(d)
    out.sort(key=lambda d: list(ids).index(d["id"]))
    return out


def mark_event_deleted(row_id):
    db.execute("UPDATE created_events SET status='deleted' WHERE id=?", (row_id,))
    db.commit()


def upcoming_created_events(limit=60):
    since = (datetime.now(MSK).date() - timedelta(days=1)).isoformat()
    rows = db.execute("SELECT * FROM created_events WHERE status='active' ORDER BY id DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        last_day = d["data"].get("date_end") or d["data"].get("date_start") or ""
        if last_day >= since:
            out.append(d)
        if len(out) >= limit:
            break
    return out


def gmail_seen_get(msg_id):
    return db.execute("SELECT * FROM gmail_seen WHERE msg_id=?", (msg_id,)).fetchone()


def gmail_seen_set(msg_id, result, attempts):
    db.execute("INSERT OR REPLACE INTO gmail_seen(msg_id, result, attempts, seen_at) VALUES (?,?,?,?)",
               (msg_id, result, attempts, _iso()))
    db.commit()


def gmail_seen_prune():
    db.execute("DELETE FROM gmail_seen WHERE seen_at < ?", (_iso(_now() - timedelta(days=30)),))
    db.commit()


# ─── Google ─────────────────────────────────────────────────────────────────

def _google_creds(scope, use_cached_token=True):
    token_data = json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    return Credentials(
        token=token_data.get("token") if use_cached_token else None,
        refresh_token=token_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=[scope],
    )


def get_calendar_service():
    creds = _google_creds(CALENDAR_SCOPE)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def get_gmail_service():
    # Отдельные учетные данные только с правом на почту: если токен выдан без Gmail,
    # падает только почта, календарь продолжает работать.
    creds = _google_creds(GMAIL_SCOPE, use_cached_token=False)
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def find_calendar_id(service, calendar_name):
    if not calendar_name:
        return "primary", None
    try:
        calendars = service.calendarList().list().execute()
        for cal in calendars.get("items", []):
            summary = cal.get("summary", "")
            if calendar_name.strip().lower() in summary.lower():
                return cal["id"], summary
    except Exception as e:
        logger.exception(e)
    return "primary", None


def _event_body(e, reminder_minutes=None):
    date_start, date_end = e.get("date_start"), e.get("date_end")
    time_start, time_end = e.get("time_start"), e.get("time_end")

    if not time_start:
        start = {"date": date_start}
        if date_end and date_end != date_start:
            end = {"date": (date.fromisoformat(date_end) + timedelta(days=1)).isoformat()}
        else:
            end = {"date": date_start}
    else:
        start = {"dateTime": f"{date_start}T{time_start}:00", "timeZone": "Europe/Moscow"}
        if time_end:
            end_date = date_end or date_start
            if not date_end and time_end < time_start:
                # Ночной рейс без date_end: конец на следующий день, иначе Google отклонит событие.
                end_date = (date.fromisoformat(date_start) + timedelta(days=1)).isoformat()
            end = {"dateTime": f"{end_date}T{time_end}:00", "timeZone": "Europe/Moscow"}
        else:
            end_dt = datetime.fromisoformat(f"{date_start}T{time_start}:00") + timedelta(hours=2)
            end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Moscow"}

    body = {"summary": e.get("title"), "start": start, "end": end}
    if e.get("location"):
        body["location"] = e["location"]
    if e.get("description"):
        body["description"] = e["description"]
    if reminder_minutes is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
    else:
        body["reminders"] = {"useDefault": True}
    return body


def create_calendar_event(e, reminder_minutes=None):
    service = get_calendar_service()
    calendar_id, matched_name = find_calendar_id(service, e.get("calendar_name"))
    event = service.events().insert(calendarId=calendar_id, body=_event_body(e, reminder_minutes)).execute()
    return {"calendar_id": calendar_id, "event_id": event["id"], "link": event.get("htmlLink"), "label": matched_name}


def delete_calendar_event(calendar_id, event_id):
    service = get_calendar_service()
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as err:
        if err.resp.status in (404, 410):  # уже удалено вручную
            return
        raise


# ─── Duplicates ─────────────────────────────────────────────────────────────

def _words(s):
    return {w for w in re.findall(r"\w+", (s or "").lower()) if len(w) >= 4}


def same_event(a, b):
    if not a.get("date_start") or a.get("date_start") != b.get("date_start"):
        return False
    if a.get("time_start") or b.get("time_start"):
        return a.get("time_start") == b.get("time_start")
    return bool(_words(a.get("title")) & _words(b.get("title")))


def is_pending_duplicate(chat_id, e):
    return any(same_event(e, pe) for card in pending_proposals(chat_id) for pe in card["events"])


def _own_calendar_ids(service):
    items = service.calendarList().list().execute().get("items", [])
    ids = [c["id"] for c in items if c.get("accessRole") == "owner"]
    return ids or ["primary"]


def _parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def calendar_has_duplicate(service, cal_ids, e):
    ds = e.get("date_start")
    if not ds:
        return False
    if e.get("time_start"):
        start = datetime.fromisoformat(f"{ds}T{e['time_start']}:00").replace(tzinfo=MSK)
        tmin, tmax = start - timedelta(minutes=1), start + timedelta(minutes=1)
    else:
        day = date.fromisoformat(ds)
        tmin = datetime(day.year, day.month, day.day, tzinfo=MSK)
        tmax = tmin + timedelta(days=1)
    for cal in cal_ids:
        items = service.events().list(calendarId=cal, timeMin=tmin.isoformat(), timeMax=tmax.isoformat(),
                                      singleEvents=True, maxResults=50).execute().get("items", [])
        for it in items:
            if it.get("status") == "cancelled":
                continue
            st = it.get("start") or {}
            if e.get("time_start"):
                if st.get("dateTime") and abs((_parse_dt(st["dateTime"]) - start).total_seconds()) < 60:
                    return True
            elif st.get("date") == ds and _words(it.get("summary")) & _words(e.get("title")):
                return True
    return False


def drop_calendar_duplicates(events):
    try:
        service = get_calendar_service()
        cal_ids = _own_calendar_ids(service)
        return [e for e in events if not calendar_has_duplicate(service, cal_ids, e)]
    except Exception as e:
        logger.exception(e)
        return events  # лучше лишняя карточка, чем потерянная бронь


# ─── Claude tools ────────────────────────────────────────────────────────────

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":         {"type": "string"},
        "date_start":    {"type": "string"},
        "date_end":      {"type": "string"},
        "date_pretty":   {"type": "string"},
        "time_start":    {"type": "string"},
        "time_end":      {"type": "string"},
        "location":      {"type": "string"},
        "description":   {"type": "string"},
        "calendar_name": {"type": "string", "description": "Название календаря, ТОЛЬКО если пользователь явно его указал. Иначе не указывай это поле вовсе."},
    },
    "required": ["title", "date_start", "date_pretty"],
}

CALENDAR_TOOL = {
    "name": "propose_calendar_events",
    "description": "Предлагает создать одно или несколько событий в Google Календаре.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {"type": "array", "items": EVENT_SCHEMA}
        },
        "required": ["events"],
    },
}

def build_system_prompt():
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    return f"""Ты умный помощник, который добавляет мероприятия в Google Календарь.

Пользователь присылает текст, скриншот или PDF. Твоя задача:

1. Извлеки все мероприятия из документа
2. Если дата относительная ("завтра", "в субботу") — вычисли абсолютную от сегодня
3. Формируй название по этим правилам:

   ДЛЯ ТРАНСПОРТА (поезд, самолёт): эмодзи + маршрут
   Примеры: "✈️ СПб → Милан", "🚂 СПб → Москва"

   ДЛЯ ВСЕГО ОСТАЛЬНОГО: эмодзи + место · событие
   Место — это название заведения, площадки, клиники, отеля.
   Событие — название фильма, спектакля, исполнителя, специалиста и т.д.
   Если место длинное — сокращай до узнаваемой формы.
   Если события нет (ресторан, отель) — только эмодзи + название места.
   Примеры: "🎬 Аврора · Грация", "🎭 БДТ · Гамлет", "🎵 Ледовый · Земфира",
            "🏥 Скандинавия · Кардиолог", "🍽️ Бюро", "🏨 Усадьба Адмирала Лазарева"

   НЕ включай в название: субтитры, версии, классы, номера мест, технические детали, язык показа.

   ВАЖНО: название формируй САМОСТОЯТЕЛЬНО по правилам выше — не копируй название из документа как есть.
   Из документа извлекай только смысловые части: название места и название события.
   Пример: в билете написано "Грация (версия с субтитрами) · Кинотеатр Ленфильм" — название должно быть "🎬 Ленфильм · Грация".

ТИПЫ СОБЫТИЙ:

А) ПОЛНОДНЕВНЫЙ БАННЕР (НЕ указывай time_start и time_end):
   — Отель, Airbnb, аренда жилья, проживание
   — Поездка, отпуск, командировка целиком
   Укажи date_start и date_end. Время заезда/выезда перенеси в description.

Б) БЛОК В СЕТКЕ ЧАСОВ (указывай time_start и time_end):
   — Перелёты и рейсы (всегда с временем, даже если через ночь)
   — Поезда
   — Театр, концерт, кино, ресторан, тренировки
   — Всё что привязано к конкретному времени

ПРАВИЛА ПОЛЯ DESCRIPTION:
ВАЖНО: не копируй текст из документа как есть. Формируй description САМОСТОЯТЕЛЬНО по правилам ниже.

Включай ТОЛЬКО то, чего нет в названии, времени и месте. Только то, что реально понадобится в нужный момент.
Смысловые блоки разделяй пустой строкой. Без лишних слов и заголовков если смысл очевиден.

НЕ включай в description: имена пассажиров, время в пути, адреса (если место уже в location), инструкции и условия перевозки, маркетинговый текст.

Примеры (следуй им точно):

Рейс:
Turkish Airlines · TK402
Пулково T1 → Стамбул IST

Ручная кладь: 8 кг
Зарегистрированный: 30 кг

Поезд:
Сапсан · 779
Вагон 08, места 031–032

Отель:
Заезд: 28 марта 14:00
Выезд: 29 марта 12:00

Номер: двухместный люкс
Бронирование: 523846996

Кино/театр/концерт:
Зал 1, ряд 7, места 14–15

Заказ: 33435100

Если дополнительных деталей нет — description не заполняй вовсе.

КАЛЕНДАРИ:
У пользователя несколько календарей, например: Основной, БСМЗ, БЦП.
По умолчанию событие идёт в Основной — НЕ указывай calendar_name.
Если рядом с сообщением, скриншотом, пересланным письмом или документом пользователь оставил короткую подпись/пометку с названием календаря или проекта (даже одно слово, например "БЦП" или "БСМЗ") — это указание на календарь, используй его как calendar_name.
Такая пометка не обязательно оформлена как полноценная инструкция — это может быть просто слово или аббревиатура, отдельно от основного текста/документа.
Если названного календаря не существует — всё равно укажи calendar_name как есть, бот сам обработает это и создаст событие в Основном, а пользователь увидит и сможет поправить.

МНОЖЕСТВЕННЫЕ СОБЫТИЯ:
— Билеты туда-обратно: два события
— Перелёт с пересадкой: два события (каждый сегмент отдельно)
— Передавай все события ОДНИМ вызовом инструмента

УТОЧНЯЮЩИЕ ВОПРОСЫ: только если информации нет вообще и без неё событие создать невозможно.

НИКОГДА не используй символы * или ** в тексте.

Сегодня: {now_msk.strftime("%Y-%m-%d, %A")} (день недели по московскому времени). Часовой пояс: Europe/Moscow. При вычислении дня недели ВСЕГДА используй московское время."""


CORRECTION_SYSTEM_PROMPT = """Ты помогаешь исправлять мероприятия, предложенные для добавления в Google Календарь.

Тебе дан JSON с текущими событиями (одно или несколько) и текст с исправлениями от пользователя.
Примени исправления и верни ОБНОВЛЁННЫЙ список событий тем же инструментом propose_calendar_events.
Меняй только то, что просит пользователь, остальное оставляй как есть.
Следуй тем же правилам оформления названий и описаний (эмодзи + место · событие, без лишних деталей и т.д.).
НИКОГДА не используй символы * или **."""

EMAIL_INSTRUCTION = """Это письмо из почты пользователя. Он его не пересылал: бот сам нашел его в почтовом ящике.
Если в письме есть его личное мероприятие с конкретной датой (бронь, билет, запись, приглашение), предложи событие по тем же правилам.
Если это перенос или изменение брони, предложи событие с новыми данными.
Если реального мероприятия нет (реклама, рассылка, анонс, чек, доставка), НЕ вызывай инструмент и ответь одним словом: НЕТ.
calendar_name не указывай."""

TRIAGE_PROMPT = """Ты фильтр входящей почты. Определи, что это за письмо для владельца ящика.

event — письмо о его личном мероприятии с конкретной датой: бронь (отель, ресторан, спорт, салон, врач, аренда), билет (самолет, поезд, автобус, концерт, театр, кино, музей, матч), запись на прием или услугу, приглашение на встречу или звонок, напоминание о такой брони, перенос или изменение такой брони. Подтверждение брони, где просто описаны условия отмены, — это event.
cancellation — письмо о том, что такая бронь, билет или встреча ОТМЕНЕНЫ (отмена бронирования, возврат билета, отмена записи или встречи).
none — все остальное: реклама, рассылки, анонсы и афиши, акции, чеки и заказы товаров, доставка, счета, уведомления сервисов, переписка без конкретного мероприятия.

Ответь только инструментом classify_email."""

TRIAGE_TOOL = {
    "name": "classify_email",
    "description": "Классифицирует входящее письмо.",
    "input_schema": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": ["event", "cancellation", "none"]}},
        "required": ["kind"],
    },
}

MATCH_PROMPT = """Пользователю пришло письмо об отмене брони, билета или встречи.
Ниже список событий, которые бот раньше добавил в его календарь; у каждого есть key.
Определи, какое из них отменяет это письмо. Сверяй дату, время, место, номер брони или заказа.
Если уверенного совпадения нет, key = "none". Ответь только инструментом match_cancelled_event."""

MATCH_TOOL = {
    "name": "match_cancelled_event",
    "description": "Указывает событие, которое отменено письмом.",
    "input_schema": {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "key события из списка или none"}},
        "required": ["key"],
    },
}


# ─── Claude processing ──────────────────────────────────────────────────────

def claude_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _tool_input(response, name):
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block.input
    return None


def _extract(system, content):
    """Синхронный вызов Claude. Возвращает (events, text)."""
    response = claude_client().messages.create(
        model=MAIN_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[CALENDAR_TOOL],
        messages=[{"role": "user", "content": content}],
    )
    data = _tool_input(response, "propose_calendar_events")
    if data is not None:
        return data.get("events") or [], None
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return [], ("\n".join(texts).strip() or None)


async def process_with_claude(text=None, image_bytes=None, pdf_bytes=None, caption=None):
    content = []
    if pdf_bytes:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode()}})
        content.append({"type": "text", "text": "Добавь мероприятия из этого документа в мой календарь."})
    elif image_bytes:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(image_bytes).decode()}})
        content.append({"type": "text", "text": "Добавь это мероприятие в мой календарь."})
    else:
        content.append({"type": "text", "text": f"Добавь в календарь:\n\n{text}"})

    if caption:
        content.append({"type": "text", "text": f"Подпись пользователя к файлу: {caption}"})

    return await asyncio.to_thread(_extract, build_system_prompt(), content)


async def process_correction(current_events, correction_text):
    prompt = (
        f"Текущие события:\n{json.dumps(current_events, ensure_ascii=False, indent=2)}\n\n"
        f"Исправление от пользователя: {correction_text}"
    )
    events, text = await asyncio.to_thread(_extract, CORRECTION_SYSTEM_PROMPT, prompt)
    if events:
        return events, None
    return None, text or "Не понял исправление."


def triage_email(mail):
    names = ", ".join(mail["attachments"]) or "нет"
    content = f"От: {mail['from']}\nТема: {mail['subject']}\nВложения: {names}\n\n{mail['text'][:2500]}"
    response = claude_client().messages.create(
        model=TRIAGE_MODEL,
        max_tokens=100,
        system=TRIAGE_PROMPT,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "classify_email"},
        messages=[{"role": "user", "content": content}],
    )
    data = _tool_input(response, "classify_email") or {}
    kind = data.get("kind")
    return kind if kind in ("event", "cancellation") else "none"


def build_email_content(mail, with_pdfs=True):
    content = []
    if with_pdfs:
        for _, data in mail["pdfs"]:
            content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                           "data": base64.standard_b64encode(data).decode()}})
    body = (f"{EMAIL_INSTRUCTION}\n\nОт: {mail['from']}\nТема: {mail['subject']}\nДата письма: {mail['date']}\n\n"
            f"{mail['text'][:20000]}")
    for name, ics in mail["ics"]:
        body += f"\n\nВложение {name}:\n{ics[:5000]}"
    content.append({"type": "text", "text": body})
    return content


def extract_from_email(mail):
    try:
        events, _ = _extract(build_system_prompt(), build_email_content(mail))
    except anthropic.BadRequestError:
        if not mail["pdfs"]:
            raise
        logger.warning("Claude не принял PDF из письма %s, пробую без вложений", mail["id"])
        events, _ = _extract(build_system_prompt(), build_email_content(mail, with_pdfs=False))
    return events


def match_cancellation(mail, candidates):
    listing = [{
        "key": str(c["id"]),
        "title": c["data"].get("title"),
        "date_start": c["data"].get("date_start"),
        "date_end": c["data"].get("date_end"),
        "time_start": c["data"].get("time_start"),
        "location": c["data"].get("location"),
        "description": (c["data"].get("description") or "")[:300],
    } for c in candidates]
    content = (f"События в календаре:\n{json.dumps(listing, ensure_ascii=False, indent=1)}\n\n"
               f"Письмо:\nОт: {mail['from']}\nТема: {mail['subject']}\nДата письма: {mail['date']}\n\n{mail['text'][:10000]}")
    response = claude_client().messages.create(
        model=MAIN_MODEL,
        max_tokens=200,
        system=MATCH_PROMPT,
        tools=[MATCH_TOOL],
        tool_choice={"type": "tool", "name": "match_cancelled_event"},
        messages=[{"role": "user", "content": content}],
    )
    key = str((_tool_input(response, "match_cancelled_event") or {}).get("key", "none"))
    valid = {str(c["id"]): c["id"] for c in candidates}
    return valid.get(key)


# ─── Format ─────────────────────────────────────────────────────────────────

def esc(s):
    return html.escape(str(s), quote=False) if s else ""


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _desc(e, limit):
    d = e.get("description")
    if not d or limit == 0:
        return None
    if limit and len(d) > limit:
        return d[:limit].rstrip() + "…"
    return d


def _event_lines(e, calendar_label, desc_limit):
    lines = [f"<b>{esc(e.get('title'))}</b>"]
    date_str = esc(e.get("date_pretty") or e.get("date_start", ""))
    time_str = e.get("time_start", "")
    if time_str:
        end_t = e.get("time_end", "")
        time_str += f" - {end_t}" if end_t else ""
        lines.append(f"{date_str}, {esc(time_str)}")
    else:
        lines.append(date_str)
    if e.get("location"):
        lines.append(esc(e["location"]))
    lines.append(f"Календарь: {esc(calendar_label or 'Основной')}")
    d = _desc(e, desc_limit)
    if d:
        lines.append(esc(d))
    return lines


def format_event_card(e, index=None, total=None, desc_limit=None):
    lines = []
    if index is not None and total and total > 1:
        lines.append(f"<b>Событие {index + 1} из {total}:</b>")
    lines += _event_lines(e, e.get("calendar_name"), desc_limit)
    return "\n".join(lines)


def format_confirmation(events, desc_limit=None):
    header = "📋 <b>Проверь детали мероприятия:</b>\n" if len(events) == 1 else "📋 <b>Проверь детали мероприятий:</b>\n"
    cards = [format_event_card(e, i, len(events), desc_limit) for i, e in enumerate(events)]
    return header + "\n\n".join(cards)


def format_event_added(e, matched_calendar_name=None, desc_limit=None):
    return "\n".join(_event_lines(e, matched_calendar_name, desc_limit))


def fit(build_text):
    """Укорачивает описания, пока сообщение не влезет в лимит Telegram."""
    text = ""
    for limit in (None, 400, 150, 0):
        text = build_text(limit)
        if len(text) <= TG_TEXT_LIMIT:
            return text
    return text


def proposal_view(events, header=None):
    body = fit(lambda lim: format_confirmation(events, lim))
    return f"{header}\n\n{body}" if header else body


def added_view(created, reminder_minutes, errors, desc_limit=None):
    reminder_text = ""
    if reminder_minutes == 120:
        reminder_text = " (напомню за 2 часа)"
    elif reminder_minutes == 1440:
        reminder_text = " (напомню за сутки)"

    blocks = []
    for _, e, res in created:
        block = f"<blockquote>{format_event_added(e, res['label'], desc_limit)}</blockquote>"
        if res.get("link"):
            block += f"\n<a href=\"{html.escape(res['link'], quote=True)}\">Открыть в Google Календаре</a>"
        blocks.append(block)

    if len(created) == 1:
        text = f"✅ Добавлено{reminder_text}!\n\n" + blocks[0]
    else:
        n = len(created)
        text = f"✅ Добавлено {n} {plural(n, 'событие', 'события', 'событий')}{reminder_text}!\n\n" + "\n".join(blocks)
    if errors:
        text += "\n\n⚠️ Не удалось добавить:\n" + "\n".join(f"{esc(e.get('title'))}: {esc(ex)}" for e, ex in errors)
    return text


def deleted_view(rows, desc_limit=None):
    blocks = [f"<blockquote>{format_event_added(r['data'], r['calendar_label'], desc_limit)}</blockquote>" for r in rows]
    return "🗑 Удалено из календаря:\n\n" + "\n".join(blocks)


def delete_question(rows):
    if len(rows) == 1:
        return f"Удалить «{esc(rows[0]['data'].get('title'))}» из календаря?"
    n = len(rows)
    return f"Удалить из календаря {n} {plural(n, 'событие', 'события', 'событий')}?"


def mail_title(mail):
    subject = mail["subject"]
    if len(subject) > 90:
        subject = subject[:90].rstrip() + "…"
    return f"{esc(mail['sender'])} · «{esc(subject)}»"


# ─── Keyboards ──────────────────────────────────────────────────────────────

def confirmation_keyboard(multi=False):
    add_label = "✅ Добавить все" if multi else "✅ Добавить"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(add_label, callback_data="add_default", style="success"), InlineKeyboardButton("❌ Отмена", callback_data="cancel", style="danger")],
        [InlineKeyboardButton("✅🔔 За 2 ч.", callback_data="add_remind_120"), InlineKeyboardButton("✅🔔 За сутки", callback_data="add_remind_1440")],
    ])


def added_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data="del_ask")]])


def cancel_offer_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data="del_ask"),
                                  InlineKeyboardButton("Оставить", callback_data="keep")]])


def delete_confirm_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Да, удалить", callback_data="del_yes", style="danger"),
                                  InlineKeyboardButton("Нет", callback_data="del_no")]])


# ─── Telegram helpers ───────────────────────────────────────────────────────

async def safe_edit(bot, chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                                    reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        raise


async def send_card(bot, chat_id, events, source, header=None):
    view = proposal_view(events, header)
    sent = await bot.send_message(chat_id, view, parse_mode="HTML",
                                  reply_markup=confirmation_keyboard(multi=len(events) > 1),
                                  link_preview_options=NO_PREVIEW)
    save_card(chat_id, sent.message_id, "proposal", source, "pending", events=events, header=header, view=view)
    return sent


# ─── Telegram handlers ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Присылай мероприятие текстом, скриншотом или PDF — "
        "я покажу что понял, и ты подтвердишь добавление в Google Календарь.\n\n"
        "После того как я покажу карточку — можешь в течение 2 минут прислать исправление текстом, и я обновлю детали. "
        "Исправить любую неподтвержденную карточку можно и позже: ответь на нее.\n\n"
        "Брони, билеты и приглашения из Gmail я присылаю сам."
    )


async def apply_correction(update, context, card):
    msg = update.message
    chat_id = msg.chat.id
    await msg.reply_text("✏️ Уточняю...")
    new_events, error = await process_correction(card["events"], msg.text)
    if not new_events:
        await msg.reply_text(error or "Не понял исправление, попробуй иначе.")
        return
    # Старая карточка больше не действует: ее кнопки не должны добавить устаревшие данные.
    update_card(chat_id, card["message_id"], status="superseded")
    try:
        await safe_edit(context.bot, chat_id, card["message_id"], card["view"] + "\n\n↓ <i>Исправлено ниже</i>")
    except Exception as e:
        logger.exception(e)
    await send_card(context.bot, chat_id, new_events, card["source"], card["header"])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    msg = update.message
    chat_id = msg.chat.id

    target = None
    if msg.reply_to_message is not None:
        card = get_card(chat_id, msg.reply_to_message.message_id)
        if card and card["kind"] == "proposal":
            if card["status"] != "pending":
                await msg.reply_text("Эта карточка уже обработана, исправить ее нельзя. Пришли событие заново.")
                return
            target = card
    else:
        card = latest_user_card(chat_id)
        if card and card["status"] == "pending" and card_age_seconds(card) <= FOLLOWUP_WINDOW_SECONDS:
            target = card

    if target:
        try:
            await apply_correction(update, context, target)
        except Exception as e:
            logger.exception(e)
            await msg.reply_text(f"Ошибка: {e}")
        return

    await msg.reply_text("🤔 Анализирую...")
    try:
        events, text_reply = await process_with_claude(text=msg.text)
        if events: await send_card(context.bot, chat_id, events, "user")
        else: await msg.reply_text(text_reply or "Не понял 🤷")
    except Exception as e:
        logger.exception(e)
        await msg.reply_text(f"Ошибка: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    await update.message.reply_text("🤔 Читаю скриншот...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        events, text_reply = await process_with_claude(image_bytes=image_bytes, caption=update.message.caption)
        if events: await send_card(context.bot, update.message.chat.id, events, "user")
        else: await update.message.reply_text(text_reply or "Не нашёл мероприятие 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Пока поддерживаю только PDF файлы.")
        return
    await update.message.reply_text("🤔 Читаю PDF...")
    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = bytes(await file.download_as_bytearray())
        events, text_reply = await process_with_claude(pdf_bytes=pdf_bytes, caption=update.message.caption)
        if events: await send_card(context.bot, update.message.chat.id, events, "user")
        else: await update.message.reply_text(text_reply or "Не нашёл мероприятия в PDF 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"Ошибка: {e}")


ADD_ACTIONS = {"add_default": None, "add_remind_120": 120, "add_remind_1440": 1440}


async def on_proposal_action(query, bot, card, data):
    chat_id, mid = card["chat_id"], card["message_id"]
    if card["kind"] != "proposal" or card["status"] != "pending":
        await query.answer("Эта карточка уже обработана.")
        return
    await query.answer()

    if data == "cancel":
        update_card(chat_id, mid, status="cancelled")
        await safe_edit(bot, chat_id, mid, "Отменено.")
        return

    reminder_minutes = ADD_ACTIONS[data]
    update_card(chat_id, mid, status="adding")  # защита от двойного нажатия
    created, errors = [], []
    for e in card["events"]:
        try:
            res = await asyncio.to_thread(create_calendar_event, e, reminder_minutes)
            row_id = add_created_event(res["calendar_id"], res["event_id"], e, res["label"], res["link"])
            created.append((row_id, e, res))
        except Exception as ex:
            logger.exception(ex)
            errors.append((e, ex))

    if not created:
        update_card(chat_id, mid, status="pending")
        reason = "; ".join(str(ex) for _, ex in errors)
        await safe_edit(bot, chat_id, mid, card["view"] + f"\n\n⚠️ Ошибка при создании: {esc(reason)}",
                        reply_markup=confirmation_keyboard(multi=len(card["events"]) > 1))
        return

    view = fit(lambda lim: added_view(created, reminder_minutes, errors, lim))
    update_card(chat_id, mid, status="added", view=view, target_ids=[row_id for row_id, _, _ in created])
    await safe_edit(bot, chat_id, mid, view, reply_markup=added_keyboard())


async def on_delete_action(query, bot, card, data):
    chat_id, mid = card["chat_id"], card["message_id"]
    kind, status = card["kind"], card["status"]
    deletable = (kind == "proposal" and status == "added") or (kind == "cancel_offer" and status == "pending")
    if not deletable:
        await query.answer("Эта карточка уже обработана.")
        return
    base_keyboard = added_keyboard() if kind == "proposal" else cancel_offer_keyboard()

    if data == "keep":
        await query.answer()
        if kind == "cancel_offer":
            update_card(chat_id, mid, status="kept")
            await safe_edit(bot, chat_id, mid, card["view"] + "\n\nОставлено в календаре.")
        return

    rows = get_created_events(card["target_ids"], only_active=True)
    if not rows:
        await query.answer("Эти события уже удалены.")
        update_card(chat_id, mid, status="deleted")
        await safe_edit(bot, chat_id, mid, card["view"])
        return

    if data == "del_ask":
        await query.answer()
        await safe_edit(bot, chat_id, mid, card["view"] + f"\n\n<b>{delete_question(rows)}</b>",
                        reply_markup=delete_confirm_keyboard())
        return

    if data == "del_no":
        await query.answer()
        await safe_edit(bot, chat_id, mid, card["view"], reply_markup=base_keyboard)
        return

    # del_yes
    await query.answer()
    deleted, errors = [], []
    for r in rows:
        try:
            await asyncio.to_thread(delete_calendar_event, r["calendar_id"], r["event_id"])
            mark_event_deleted(r["id"])
            deleted.append(r)
        except Exception as ex:
            logger.exception(ex)
            errors.append((r, ex))

    if errors:
        failed = "\n".join(f"{esc(r['data'].get('title'))}: {esc(ex)}" for r, ex in errors)
        done = f"\n\n🗑 Удалено: {len(deleted)}" if deleted else ""
        await safe_edit(bot, chat_id, mid, card["view"] + f"{done}\n\n⚠️ Не удалось удалить:\n{failed}",
                        reply_markup=base_keyboard)
        return

    view = fit(lambda lim: deleted_view(deleted, lim))
    update_card(chat_id, mid, status="deleted", view=view)
    await safe_edit(bot, chat_id, mid, view)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    query = update.callback_query
    card = get_card(query.message.chat.id, query.message.message_id)
    if card is None:
        await query.answer("Карточка устарела: пришли событие заново.", show_alert=True)
        return
    try:
        if query.data == "cancel" or query.data in ADD_ACTIONS:
            await on_proposal_action(query, context.bot, card, query.data)
        elif query.data in ("del_ask", "del_no", "del_yes", "keep"):
            await on_delete_action(query, context.bot, card, query.data)
        else:
            await query.answer()
    except Exception as e:
        logger.exception(e)
        await context.bot.send_message(query.message.chat.id, f"Ошибка: {e}")


# ─── Gmail ──────────────────────────────────────────────────────────────────

class _HTMLText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _squeeze(text):
    out, blank = [], False
    for line in text.splitlines():
        line = re.sub(r"[ \t ​‌]+", " ", line).strip()
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def html_to_text(s):
    p = _HTMLText()
    try:
        p.feed(s)
        p.close()
    except Exception:
        pass
    return _squeeze("".join(p.parts))


def _b64url(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_body(part, data=None):
    data = data or (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = _b64url(data)
    charset = "utf-8"
    for h in part.get("headers", []):
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w\-]+)', h.get("value", ""), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _walk(part, acc):
    if part.get("parts"):
        for p in part["parts"]:
            _walk(p, acc)
        return
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    ref = {"filename": filename, "attachment_id": body.get("attachmentId"), "data": body.get("data"),
           "size": body.get("size", 0), "part": part}
    if mime == "text/calendar" or filename.lower().endswith(".ics"):
        ref["filename"] = filename or "invite.ics"
        acc["ics_refs"].append(ref)
        if filename:
            acc["attachments"].append(filename)
        return
    if filename:
        acc["attachments"].append(filename)
        if mime == "application/pdf" or filename.lower().endswith(".pdf"):
            acc["pdf_refs"].append(ref)
        return
    if mime == "text/plain":
        acc["plain"].append(_decode_body(part))
    elif mime == "text/html":
        acc["html"].append(_decode_body(part))


def parse_gmail_message(msg):
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    acc = {"plain": [], "html": [], "pdf_refs": [], "ics_refs": [], "attachments": []}
    _walk(payload, acc)
    plain = _squeeze("\n".join(acc["plain"]))
    html_text = html_to_text("\n".join(acc["html"]))
    # HTML — основная версия письма; текстовая часть часто заглушка «откройте в браузере».
    text = html_text if html_text and len(html_text) * 3 >= len(plain) else plain
    text = text or msg.get("snippet", "")
    name, addr = parseaddr(headers.get("from", ""))
    return {
        "id": msg["id"],
        "from": headers.get("from", ""),
        "sender": name or addr or "?",
        "subject": headers.get("subject") or "(без темы)",
        "date": headers.get("date", ""),
        "text": text,
        "attachments": acc["attachments"],
        "pdf_refs": acc["pdf_refs"],
        "ics_refs": acc["ics_refs"],
        "pdfs": [],
        "ics": [],
    }


def _attachment_data(service, msg_id, ref):
    if ref.get("data"):
        return ref["data"]
    if not ref.get("attachment_id"):
        return None
    att = service.users().messages().attachments().get(userId="me", messageId=msg_id, id=ref["attachment_id"]).execute()
    return att.get("data")


def load_attachments(service, mail):
    for ref in mail["pdf_refs"][:2]:
        if (ref.get("size") or 0) > GMAIL_MAX_PDF_BYTES:
            continue
        data = _attachment_data(service, mail["id"], ref)
        if data:
            mail["pdfs"].append((ref["filename"], _b64url(data)))
    for ref in mail["ics_refs"][:2]:
        data = _attachment_data(service, mail["id"], ref)
        if data:
            mail["ics"].append((ref["filename"], _decode_body(ref["part"], data)))
    return mail


def gmail_list_recent_ids(service):
    ids, token = [], None
    for _ in range(3):
        kwargs = {"userId": "me", "q": GMAIL_QUERY, "maxResults": 100}
        if token:
            kwargs["pageToken"] = token
        resp = service.users().messages().list(**kwargs).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def gmail_get_message(service, msg_id):
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


async def handle_mail_event(bot, service, mail):
    await asyncio.to_thread(load_attachments, service, mail)
    events = await asyncio.to_thread(extract_from_email, mail)
    if not events:
        return "no_events"
    fresh = [e for e in events if not is_pending_duplicate(OWNER_ID, e)]
    if fresh:
        fresh = await asyncio.to_thread(drop_calendar_duplicates, fresh)
    if not fresh:
        return "duplicate"
    await send_card(bot, OWNER_ID, fresh, "gmail", header=f"📧 Из письма: {mail_title(mail)}")
    return "card"


async def handle_mail_cancellation(bot, mail):
    candidates = upcoming_created_events()
    if not candidates:
        return "cancel_nothing_to_match"
    row_id = await asyncio.to_thread(match_cancellation, mail, candidates)
    rows = get_created_events([row_id], only_active=True) if row_id else []
    if not rows:
        return "cancel_no_match"
    r = rows[0]
    if has_pending_cancel_offer(r["id"]):
        return "cancel_already_offered"
    view = (f"📧 Отмена брони: {mail_title(mail)}\n\nЭто событие есть в календаре:\n"
            f"<blockquote>{format_event_added(r['data'], r['calendar_label'])}</blockquote>")
    sent = await bot.send_message(OWNER_ID, view, parse_mode="HTML", reply_markup=cancel_offer_keyboard(),
                                  link_preview_options=NO_PREVIEW)
    save_card(OWNER_ID, sent.message_id, "cancel_offer", "gmail", "pending", view=view, target_ids=[r["id"]])
    return "cancel_offer"


async def process_gmail_message(bot, service, msg_id, since):
    msg = await asyncio.to_thread(gmail_get_message, service, msg_id)
    if int(msg.get("internalDate", "0")) // 1000 < since:
        return "old"
    labels = set(msg.get("labelIds", []))
    if "DRAFT" in labels or ("SENT" in labels and "INBOX" not in labels):
        return "own"
    mail = parse_gmail_message(msg)
    kind = await asyncio.to_thread(triage_email, mail)
    if kind == "event":
        return await handle_mail_event(bot, service, mail)
    if kind == "cancellation":
        return await handle_mail_cancellation(bot, mail)
    return "none"


async def warn_gmail_access(bot, err):
    logger.warning("Нет доступа к Gmail: %s", err)
    try:
        refresh_token = json.loads(os.environ["GOOGLE_TOKEN_JSON"]).get("refresh_token", "")
    except Exception:
        refresh_token = ""
    fingerprint = hashlib.sha256(refresh_token.encode()).hexdigest()[:16]
    if meta_get("gmail_warned") == fingerprint:
        return
    meta_set("gmail_warned", fingerprint)
    await bot.send_message(
        OWNER_ID,
        "📧 Не могу читать почту: Google не дает доступ. Скорее всего, в Google-токене нет разрешения на Gmail. "
        "Календарь работает как обычно.",
    )


async def gmail_poll_once(bot):
    try:
        service = await asyncio.to_thread(get_gmail_service)
        ids = await asyncio.to_thread(gmail_list_recent_ids, service)
    except RefreshError as e:
        await warn_gmail_access(bot, e)
        return
    except HttpError as e:
        if e.resp.status in (401, 403):
            await warn_gmail_access(bot, e)
            return
        raise
    if meta_get("gmail_warned"):
        meta_set("gmail_warned", None)

    since = meta_get("gmail_since")
    if since is None:
        # Первый запуск: старые письма не трогаем, иначе придет пачка карточек за прошлые дни.
        meta_set("gmail_since", int(time.time()))
        await bot.send_message(OWNER_ID, "📧 Почта подключена. Слежу за новыми письмами с этого момента.")
        return
    since = int(since)

    for msg_id in reversed(ids):  # от старых к новым
        seen = gmail_seen_get(msg_id)
        if seen and (seen["result"] != "error" or seen["attempts"] >= GMAIL_MAX_ATTEMPTS):
            continue
        attempts = (seen["attempts"] if seen else 0) + 1
        try:
            result = await process_gmail_message(bot, service, msg_id, since)
        except Exception as e:
            logger.exception("Gmail %s: %s", msg_id, e)
            result = "error"
        gmail_seen_set(msg_id, result, attempts)
    gmail_seen_prune()


async def gmail_loop(bot):
    await asyncio.sleep(10)
    while True:
        try:
            await gmail_poll_once(bot)
        except Exception as e:
            logger.exception(e)
        await asyncio.sleep(GMAIL_POLL_MINUTES * 60)


_background_tasks = set()


async def post_init(application):
    if GMAIL_POLL_MINUTES > 0 and OWNER_ID:
        task = asyncio.create_task(gmail_loop(application.bot))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        logger.info("Gmail: проверка раз в %s мин", GMAIL_POLL_MINUTES)


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot started!")
    app.run_polling()
