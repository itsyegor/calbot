# 📅 Telegram → Google Calendar Bot

Бот принимает текст или скриншоты мероприятий и автоматически создаёт события в Google Календаре.

---

## Шаг 1 — Получи токен Telegram

1. Открой Telegram → найди **@BotFather**
2. Напиши `/newbot`
3. Придумай имя (например: `My Calendar Bot`)
4. Придумай username (например: `mycal_bot`) — должен заканчиваться на `bot`
5. Скопируй токен вида `7123456789:AAF...`

---

## Шаг 2 — Получи Anthropic API ключ

1. Зайди на https://console.anthropic.com
2. Войди / зарегистрируйся
3. Перейди в **API Keys** → **Create Key**
4. Скопируй ключ вида `sk-ant-...`

---

## Шаг 3 — Получи Google токен (один раз на компьютере)

1. Положи скачанный файл `client_secret.json` в папку с ботом
2. Установи зависимости:
   ```
   pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
   ```
3. Запусти:
   ```
   python get_token.py
   ```
4. Откроется браузер → войди в свой Google аккаунт → разреши доступ
5. В терминале появится длинная строка JSON — скопируй её целиком

---

## Шаг 4 — Задеплой на Railway

1. Зайди на https://railway.app → войди через GitHub
2. Нажми **"New Project"** → **"Deploy from GitHub repo"**
   - Если нет репозитория: нажми **"Empty Project"** → **"Add Service"** → загрузи папку
3. В проекте нажми на сервис → вкладка **"Variables"** → добавь:

   | Переменная | Значение |
   |---|---|
   | `TELEGRAM_TOKEN` | токен от BotFather |
   | `ANTHROPIC_API_KEY` | ключ от Anthropic |
   | `GOOGLE_TOKEN_JSON` | JSON строка из get_token.py |

4. Перейди на вкладку **"Settings"** → найди **"Start Command"** → впиши: `python bot.py`
5. Нажми **Deploy**

---

## Готово! 🎉

Открой своего бота в Telegram и пришли ему:
- Текст: `Концерт Земфиры 15 апреля в 20:00, Ледовый дворец`
- Или скриншот афиши

Бот создаст событие и пришлёт ссылку на него в Google Календаре.
