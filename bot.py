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

# ─── Google Calendar ────────────────────────────────────────────────────────

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

    # Multi-day all-day event
    if not time_start:
        start = {"date": date_start}
        # For all-day multi-day: end date is exclusive in Google Calendar
        if date_end and date_end != date_start:
            # add one day to end date
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
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    if reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": reminder_minutes}]
        }
    else:
        body["reminders"] = {"useDefault": True}

    event = service.events().insert(calendarId="primary", body=body).execute()
    return event.get("htmlLink"), None


# ─── Claude tool ────────────────────────────────────────────────────────────

CALENDAR_TOOL = {
    "name": "propose_calendar_event",
    "description": "Предлагает создать событие в Google Календаре — показывает пользователю детали для подтверждения",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":        {"type": "string", "description": "Название с тематическим эмодзи в начале"},
            "date_start":   {"type": "string", "description": "Дата начала YYYY-MM-DD"},
            "date_end":     {"type": "string", "description": "Дата окончания YYYY-MM-DD (если отличается от date_start — для многодневных событий)"},
            "date_pretty":  {"type": "string", "description": "Период в читаемом виде, например '25 апреля' или '25 апреля — 3 мая'"},
            "time_start":   {"type": "string", "description": "Время начала HH:MM (если известно)"},
            "time_end":     {"type": "string", "description": "Время окончания HH:MM (если известно)"},
            "location":     {"type": "string", "description": "Место проведения"},
            "description":  {"type": "string", "description": "Детали: маршрут перелёта, номера рейсов, аэропорты, терминалы, пересадки, адрес отеля и т.д."},
        },
        "required": ["title", "date_start", "date_pretty"],
    },
}

SYSTEM_PROMPT = f"""Ты умный помощник, который добавляет мероприятия в Google Календарь.

Пользователь присылает текст, скриншот или PDF. Твоя задача:
1. Извлеки название, дату начала, дату окончания (если многодневное), время, место
2. Если дата относительная ("завтра", "в субботу") — вычисли абсолютную от сегодня
3. В начале названия ВСЕГДА ставь тематический эмодзи (например: театр — 🎭, концерт — 🎵, кино — 🎬, спорт — 🏃, ресторан — 🍽, поездка — ✈️, выставка — 🎨, отель — 🏨, перелёт — ✈️, врач — 🏥 и т.д.)
4. Для МНОГОДНЕВНЫХ событий (отель, аренда, поездка, конференция): обязательно укажи date_end
5. Для ПЕРЕЛЁТОВ: в description укажи все детали — рейсы, аэропорты, терминалы, время вылета/прилёта, пересадки
6. Вызови инструмент propose_calendar_event
7. Если это не мероприятие — просто ответь текстом
8. НИКОГДА не используй символы ** для выделения текста

Сегодня: {datetime.now().strftime("%Y-%m-%d, %A")}. Часовой пояс: Europe/Moscow."""


# ─── Pending events ─────────────────────────────────────────────────────────

pending_events = {}  # user_id -> event_data


# ─── Claude processing ──────────────────────────────────────────────────────

async def process_with_claude(text=None, image_bytes=None, pdf_bytes=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content = []
    if pdf_bytes:
        content.append({"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode(),
        }})
        content.append({"type": "text", "text": "Найди в этом PDF мероприятие и добавь его в мой календарь."})
    elif image_bytes:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(image_bytes).decode(),
        }})
        content.append({"type": "text", "text": "Добавь это мероприятие в мой календарь."})
    else:
        content.append({"type": "text", "text": f"Добавь в календарь:\n\n{text}"})

    messages = [{"role": "user", "content": content}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=[CALENDAR_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use" and block.name == "propose_calendar_event":
                    return block.input, None
            messages.append({"role": "assistant", "content": response.content})
        else:
            for block in response.content:
                if hasattr(block, "text"):
                    return None, block.text
            return None, "Готово!"


# ─── Format confirmation ────────────────────────────────────────────────────

def format_confirmation(e: dict) -> str:
    lines = ["📋 <b>Проверь детали мероприятия:</b>\n"]
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


def confirmation_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Добавить", callback_data="add_default"),
            InlineKeyboardButton("🔔 За 2 часа", callback_data="add_remind_120"),
        ],
        [
            InlineKeyboardButton("🔔 За сутки", callback_data="add_remind_1440"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
        ],
    ])


# ─── Reply helper ───────────────────────────────────────────────────────────

async def send_confirmation(message, event_data):
    pending_events[message.chat.id] = event_data
    await message.reply_text(
        format_confirmation(event_data),
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(),
    )


# ─── Telegram handlers ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Присылай мероприятие текстом, скриншотом или PDF — "
        "я покажу что понял, и ты подтвердишь добавление в Google Календарь."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    await update.message.reply_text("🤔 Анализирую...")
    try:
        event_data, text_reply = await process_with_claude(text=update.message.text)
        if event_data:
            await send_confirmation(update.message, event_data)
        else:
            await update.message.reply_text(text_reply or "Не понял 🤷")
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
        event_data, text_reply = await process_with_claude(image_bytes=image_bytes)
        if event_data:
            await send_confirmation(update.message, event_data)
        else:
            await update.message.reply_text(text_reply or "Не нашёл мероприятие 🤷")
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
        event_data, text_reply = await process_with_claude(pdf_bytes=pdf_bytes)
        if event_data:
            await send_confirmation(update.message, event_data)
        else:
            await update.message.reply_text(text_reply or "Не нашёл мероприятие в PDF 🤷")
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
        event_data = pending_events.pop(user_id, None)
        if not event_data:
            await query.edit_message_text("Мероприятие не найдено, попробуй ещё раз.")
            return

        reminder_minutes = None
        if query.data == "add_remind_120":
            reminder_minutes = 120
        elif query.data == "add_remind_1440":
            reminder_minutes = 1440

        try:
            link, error = create_calendar_event(
                title=event_data.get("title"),
                date_start=event_data.get("date_start"),
                date_end=event_data.get("date_end"),
                time_start=event_data.get("time_start"),
                time_end=event_data.get("time_end"),
                location=event_data.get("location"),
                description=event_data.get("description"),
                reminder_minutes=reminder_minutes,
            )
            if link:
                reminder_text = ""
                if reminder_minutes == 120:
                    reminder_text = " (напомню за 2 часа)"
                elif reminder_minutes == 1440:
                    reminder_text = " (напомню за сутки)"
                await query.edit_message_text(
                    f"✅ <b>{event_data.get('title')}</b> добавлено{reminder_text}!\n\n"
                    f"<a href='{link}'>Открыть в Google Календаре</a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await query.edit_message_text(f"Ошибка: {error}")
        except Exception as e:
            logger.exception(e)
            await query.edit_message_text(f"Ошибка при создании: {e}")


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot started!")
    app.run_polling()
