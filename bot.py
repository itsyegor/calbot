import os
import json
import base64
import logging
from datetime import datetime
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

async def is_owner(update):
    if update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("Этот бот личный.")
        return False
    return True

def get_calendar_service():
    token_data = json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)

def create_calendar_event(title, date_start, date_end=None, time_start=None, time_end=None,
                          location=None, description=None, reminder_minutes=None):
    service = get_calendar_service()
    if not time_start:
        start = {"date": date_start}
        if date_end and date_end != date_start:
            from datetime import date, timedelta
            end_dt = date.fromisoformat(date_end) + timedelta(days=1)
            end = {"date": end_dt.isoformat()}
        else:
            end = {"date": date_start}
    else:
        start = {"dateTime": f"{date_start}T{time_start}:00", "timeZone": "Europe/Moscow"}
        if time_end:
            end_date = date_end if date_end else date_start
            end = {"dateTime": f"{end_date}T{time_end}:00", "timeZone": "Europe/Moscow"}
        else:
            from datetime import datetime as dt, timedelta
            start_dt = dt.fromisoformat(f"{date_start}T{time_start}:00")
            end_dt = start_dt + timedelta(hours=2)
            end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Moscow"}
    body = {"summary": title, "start": start, "end": end}
    if location: body["location"] = location
    if description: body["description"] = description
    if reminder_minutes is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
    else:
        body["reminders"] = {"useDefault": True}
    event = service.events().insert(calendarId="primary", body=body).execute()
    return event.get("htmlLink"), None

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":        {"type": "string"},
        "date_start":   {"type": "string"},
        "date_end":     {"type": "string"},
        "date_pretty":  {"type": "string"},
        "time_start":   {"type": "string"},
        "time_end":     {"type": "string"},
        "location":     {"type": "string"},
        "description":  {"type": "string"},
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

SYSTEM_PROMPT = f"""Ты умный помощник, который добавляет мероприятия в Google Календарь.

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

   НЕ включай в название: субтитры, версии, классы, номера мест, технические детали.

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
Пиши только то, чего нет в названии, времени и месте. Только то, что понадобится в нужный момент.
Смысловые блоки разделяй пустой строкой. Без заголовков если смысл очевиден. Без лишних слов.

Примеры:

Рейс:
Turkish Airlines · TK402
Пулково T1 → Стамбул IST

Ручная кладь: 8 кг
Зарегистрированный: 30 кг

Отель:
Заезд: 28 марта 14:00
Выезд: 29 марта 12:00

Номер: двухместный люкс
Бронирование: 523846996

Театр/концерт:
Амфитеатр, ряд 1, места 3–4

Заказ: 38031252410

Если дополнительных деталей нет — description не заполняй вовсе.

МНОЖЕСТВЕННЫЕ СОБЫТИЯ:
— Билеты туда-обратно: два события
— Перелёт с пересадкой: два события (каждый сегмент отдельно)
— Передавай все события ОДНИМ вызовом инструмента

УТОЧНЯЮЩИЕ ВОПРОСЫ: только если информации нет вообще и без неё событие создать невозможно.

НИКОГДА не используй символы * или ** в тексте.

Сегодня: {datetime.now().strftime("%Y-%m-%d, %A")}. Часовой пояс: Europe/Moscow."""

pending_events = {}

async def process_with_claude(text=None, image_bytes=None, pdf_bytes=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    content = []
    if pdf_bytes:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode()}})
        content.append({"type": "text", "text": "Добавь мероприятия из этого документа в мой календарь."})
    elif image_bytes:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(image_bytes).decode()}})
        content.append({"type": "text", "text": "Добавь это мероприятие в мой календарь."})
    else:
        content.append({"type": "text", "text": f"Добавь в календарь:\n\n{text}"})
    messages = [{"role": "user", "content": content}]
    while True:
        response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1500, system=SYSTEM_PROMPT, tools=[CALENDAR_TOOL], messages=messages)
        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use" and block.name == "propose_calendar_events":
                    return block.input.get("events", []), None
            messages.append({"role": "assistant", "content": response.content})
        else:
            for block in response.content:
                if hasattr(block, "text"):
                    return [], block.text
            return [], "Готово!"

def format_event_card(e, index=None, total=None):
    lines = []
    if index is not None and total and total > 1:
        lines.append(f"<b>Событие {index + 1} из {total}:</b>")
    lines.append(f"📌 <b>{e.get('title')}</b>")
    date_str = e.get("date_pretty") or e.get("date_start", "")
    time_str = e.get("time_start", "")
    if time_str:
        end_t = e.get("time_end", "")
        time_str += f" - {end_t}" if end_t else ""
        lines.append(f"📅 {date_str}, {time_str}")
    else:
        lines.append(f"📅 {date_str}")
    if e.get("location"):
        lines.append(f"📍 {e['location']}")
    if e.get("description"):
        lines.append(f"💬 {e['description']}")
    return "\n".join(lines)

def format_confirmation(events):
    header = "📋 <b>Проверь детали мероприятия:</b>\n" if len(events) == 1 else "📋 <b>Проверь детали мероприятий:</b>\n"
    cards = [format_event_card(e, i, len(events)) for i, e in enumerate(events)]
    return header + "\n\n".join(cards)

def confirmation_keyboard(multi=False):
    add_label = "✅ Добавить все" if multi else "✅ Добавить"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(add_label, callback_data="add_default"), InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        [InlineKeyboardButton("✅🔔 За 2 ч.", callback_data="add_remind_120"), InlineKeyboardButton("✅🔔 За сутки", callback_data="add_remind_1440")],
    ])

async def send_confirmation(message, events):
    pending_events[message.chat.id] = events
    await message.reply_text(format_confirmation(events), parse_mode="HTML", reply_markup=confirmation_keyboard(multi=len(events) > 1))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Присылай мероприятие текстом, скриншотом или PDF — я покажу что понял, и ты подтвердишь добавление в Google Календарь.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    await update.message.reply_text("🤔 Анализирую...")
    try:
        events, text_reply = await process_with_claude(text=update.message.text)
        if events: await send_confirmation(update.message, events)
        else: await update.message.reply_text(text_reply or "Не понял 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    await update.message.reply_text("🤔 Читаю скриншот...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        events, text_reply = await process_with_claude(image_bytes=image_bytes)
        if events: await send_confirmation(update.message, events)
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
        events, text_reply = await process_with_claude(pdf_bytes=pdf_bytes)
        if events: await send_confirmation(update.message, events)
        else: await update.message.reply_text(text_reply or "Не нашёл мероприятия в PDF 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"Ошибка: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if query.data == "cancel":
        pending_events.pop(user_id, None)
        await query.edit_message_text("Отменено.")
        return
    if query.data.startswith("add_"):
        events = pending_events.pop(user_id, None)
        if not events:
            await query.edit_message_text("Мероприятие не найдено, попробуй ещё раз.")
            return
        reminder_minutes = None
        if query.data == "add_remind_120": reminder_minutes = 120
        elif query.data == "add_remind_1440": reminder_minutes = 1440
        try:
            links = []
            for e in events:
                link, error = create_calendar_event(title=e.get("title"), date_start=e.get("date_start"), date_end=e.get("date_end"), time_start=e.get("time_start"), time_end=e.get("time_end"), location=e.get("location"), description=e.get("description"), reminder_minutes=reminder_minutes)
                if link: links.append((e.get("title"), link))
            reminder_text = ""
            if reminder_minutes == 120: reminder_text = " (напомню за 2 часа)"
            elif reminder_minutes == 1440: reminder_text = " (напомню за сутки)"
            if len(links) == 1:
                title, link = links[0]
                text = f"✅ <b>{title}</b> добавлено{reminder_text}!\n\n<a href='{link}'>Открыть в Google Календаре</a>"
            else:
                lines = [f"✅ Добавлено {len(links)} события{reminder_text}!\n"]
                for title, link in links:
                    lines.append(f"<a href='{link}'>{title}</a>")
                text = "\n".join(lines)
            await query.edit_message_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.exception(e)
            await query.edit_message_text(f"Ошибка при создании: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot started!")
    app.run_polling()
