
import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Настройки
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = 390640171


# Проверка доступа
def is_allowed(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == ALLOWED_USER_ID
        and update.effective_chat is not None
        and update.effective_chat.type == "private"
    )


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Привет! Я Мой GPT.\n"
        "Соединение с сервером работает!\n"
        "Доступ разрешён."
    )


# Обработка сообщений
async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    user_text = update.message.text

    await update.message.reply_text(
        f"Получил сообщение: {user_text}"
    )


# Запуск
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message)
    )

    print("Мой GPT запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
