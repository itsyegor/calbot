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

def create_calendar_event(title, date, time_start=None, time_end=None, location=None, description=None):
    service = get_calendar_service()
    if date and time_start:
        start = {"dateTime": f"{date}T{time_start}:00", "timeZone": "Europe/Moscow"}
        if time_end:
            end = {"dateTime": f"{date}T{time_end}:00", "timeZone": "Europe/Moscow"}
        else:
            h, m = map(int, time_start.split(":"))
            h = (h + 2) % 24
            end = {"dateTime": f"{date}T{h:02d}:{m:02d}:00", "timeZone": "Europe/Moscow"}
    elif date:
        start = {"date": date}
        end = {"date": date}
    else:
        return None, "Не удалось определить дату"
    body = {"summary": title, "start": start, "end": end}
    if location: body["location"] = location
    if description: body["description"] = description
    event = service.events().insert(calendarId="primary", body=body).execute()
    return event.get("htmlLink"), None


# ─── Claude tools ────────────────────────────────────────────────────────────

CALENDAR_TOOL = {
    "name": "propose_calendar_event",
    "description": "Предлагает создать событие в Google Календаре — показывает пользователю детали для подтверждения",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":       {"type": "string", "description": "Чистое название мероприятия"},
            "date":        {"type": "string", "description": "Дата YYYY-MM-DD"},
            "date_pretty": {"type": "string", "description": "Дата в читаемом виде, например «25 апреля 2025»"},
            "time_start":  {"type": "string", "description": "Время начала HH:MM"},
            "time_end":    {"type": "string", "description": "Время окончания HH:MM"},
            "location":    {"type": "string", "description": "Место проведения"},
            "description": {"type": "string", "description": "Краткое описание"},
        },
        "required": ["title", "date", "date_pretty"],
    },
}

SYSTEM_PROMPT = f"""Ты умный помощник, который добавляет мероприятия в Google Календарь.

Пользователь присылает текст или скриншот. Твоя задача:
1. Извлеки название, дату, время, место
2. Если дата относительная ("завтра", "в субботу") — вычисли абсолютную от сегодня
3. Составь чистое лаконичное название (без номеров мест, скобок и мусора)
4. Вызови инструмент propose_calendar_event — пользователь сам подтвердит создание
5. Если это не мероприятие — просто ответь текстом

Сегодня: {datetime.now().strftime("%Y-%m-%d, %A")}. Часовой пояс: Europe/Moscow."""


# ─── Pending events storage (in-memory) ────────────────────────────────────

pending_events = {}  # user_id -> event_data


# ─── Claude processing ──────────────────────────────────────────────────────

async def process_with_claude(text=None, image_bytes=None):
    """Returns (event_data | None, text_response)"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content = []
    if image_bytes:
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
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=[CALENDAR_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use" and block.name == "propose_calendar_event":
                    return block.input, None
            # shouldn't happen
            messages.append({"role": "assistant", "content": response.content})
        else:
            for block in response.content:
                if hasattr(block, "text"):
                    return None, block.text
            return None, "Готово!"


# ─── Format confirmation message ────────────────────────────────────────────

def format_confirmation(e: dict) -> str:
    lines = ["📋 <b>Проверь детали мероприятия:</b>\n"]
    lines.append(f"📌 <b>{e.get('title')}</b>")
    date_str = e.get("date_pretty") or e.get("date", "")
    time_str = e.get("time_start", "")
    if time_str:
        end = e.get("time_end", "")
        time_str += f" - {end}" if end else ""
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
            InlineKeyboardButton("✅ Добавить", callback_data="confirm_add"),
            InlineKeyboardButton("❌ Отмена",   callback_data="confirm_cancel"),
        ]
    ])


# ─── Telegram handlers ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Присылай мероприятие текстом или скриншотом — "
        "я покажу что понял, и ты подтвердишь добавление в Google Календарь."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Анализирую...")
    try:
        event_data, text_reply = await process_with_claude(text=update.message.text)
        if event_data:
            pending_events[update.effective_user.id] = event_data
            await update.message.reply_text(
                format_confirmation(event_data),
                parse_mode="HTML",
                reply_markup=confirmation_keyboard(),
            )
        else:
            await update.message.reply_text(text_reply or "Не понял 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Читаю скриншот...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        event_data, text_reply = await process_with_claude(image_bytes=image_bytes)
        if event_data:
            pending_events[update.effective_user.id] = event_data
            await update.message.reply_text(
                format_confirmation(event_data),
                parse_mode="HTML",
                reply_markup=confirmation_keyboard(),
            )
        else:
            await update.message.reply_text(text_reply or "Не нашёл мероприятие на скриншоте 🤷")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "confirm_add":
        event_data = pending_events.pop(user_id, None)
        if not event_data:
            await query.edit_message_text("⚠️ Мероприятие не найдено, попробуй ещё раз.")
            return
        try:
            link, error = create_calendar_event(
                title=event_data.get("title"),
                date=event_data.get("date"),
                time_start=event_data.get("time_start"),
                time_end=event_data.get("time_end"),
                location=event_data.get("location"),
                description=event_data.get("description"),
            )
            if link:
                await query.edit_message_text(
                    f"✅ <b>{event_data.get('title')}</b> добавлено в календарь!\n\n"
                    f"<a href='{link}'>Открыть в Google Календаре</a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await query.edit_message_text(f"❌ Ошибка: {error}")
        except Exception as e:
            logger.exception(e)
            await query.edit_message_text(f"❌ Ошибка при создании: {e}")

    elif query.data == "confirm_cancel":
        pending_events.pop(user_id, None)
        await query.edit_message_text("🚫 Отменено. Пришли другое мероприятие.")


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot started!")
    app.run_polling()
