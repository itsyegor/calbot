import os
import json
import base64
import logging
from datetime import datetime
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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
        return "Ошибка: не удалось определить дату"
    body = {"summary": title, "start": start, "end": end}
    if location: body["location"] = location
    if description: body["description"] = description
    event = service.events().insert(calendarId="primary", body=body).execute()
    return event.get("htmlLink", "Событие создано")


# ─── Claude tool ────────────────────────────────────────────────────────────

CALENDAR_TOOL = {
    "name": "create_calendar_event",
    "description": "Создаёт событие в Google Календаре пользователя",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Чистое название мероприятия"},
            "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
            "time_start": {"type": "string", "description": "Время начала HH:MM"},
            "time_end": {"type": "string", "description": "Время окончания HH:MM"},
            "location": {"type": "string", "description": "Место проведения"},
            "description": {"type": "string", "description": "Описание"},
        },
        "required": ["title", "date"],
    },
}

SYSTEM_PROMPT = f"""Ты умный помощник, который добавляет мероприятия в Google Календарь.

Пользователь присылает текст или скриншот. Твоя задача:
1. Извлеки название, дату, время, место
2. Если дата относительная ("завтра", "в субботу") — вычисли абсолютную от сегодня
3. Составь чистое лаконичное название (без номеров мест, скобок и мусора)
4. Вызови инструмент create_calendar_event
5. После — ответь кратко по-русски: что добавил, когда и где

Сегодня: {datetime.now().strftime("%Y-%m-%d, %A")}. Часовой пояс: Europe/Moscow."""


# ─── Agentic processing ─────────────────────────────────────────────────────

async def process_with_claude(text=None, image_bytes=None):
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
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "create_calendar_event":
                    try:
                        link = create_calendar_event(**block.input)
                        result = {"status": "success", "link": link}
                    except Exception as e:
                        result = {"status": "error", "message": str(e)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Готово!"


# ─── Telegram ───────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Присылай мероприятие текстом или скриншотом — "
        "Claude сам разберётся и добавит в твой Google Календарь."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Думаю...")
    try:
        reply = await process_with_claude(text=update.message.text)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Читаю скриншот...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        reply = await process_with_claude(image_bytes=image_bytes)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot started!")
    app.run_polling()
