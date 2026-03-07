#!/usr/bin/env python3
"""
Telegram Expense Tracker Bot v2
- Persistent bottom keyboard «➕ Додати витрату»
- Chain multiple expenses in one session
- Auto EUR conversion via Frankfurter API
- Writes to Google Sheets: Дата | ПІБ | Категорія | Стаття | Сума | Валюта | Разом (EUR)
"""

import os
import json
import logging
import requests
from typing import Optional
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
import gspread
from google.oauth2.service_account import Credentials

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
CATEGORY, ARTICLE, AMOUNT, NEXT_ACTION = range(4)

# ─── Categories ───────────────────────────────────────────────────────────────
CATEGORIES = [
    "Продукти",
    "Заклади",
    "Обіди на роботі",
    "Одяг",
    "Навчання",
    "Здоров'я",
    "Квартира",
    "Відпочинок",
    "Проїзд",
    "Подарунки",
    "Телефон, інтернет",
    "Обладнання",
    "Інше",
]

SHEET_HEADERS = [
    "Дата", "ПІБ", "Категорія", "Стаття", "Сума", "Валюта", "Разом (EUR)"
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─── Persistent bottom keyboard ───────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["➕ Додати витрату"]],
    resize_keyboard=True,
    is_persistent=True,
)


# ─── Google Sheets ────────────────────────────────────────────────────────────
def get_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable is not set")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID environment variable is not set")

    worksheet = client.open_by_key(sheet_id).sheet1

    # Auto-create bold headers if the sheet is empty
    if not worksheet.row_values(1):
        worksheet.append_row(SHEET_HEADERS)
        worksheet.format(
            "A1:G1",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.18, "green": 0.55, "blue": 0.34},
            },
        )
        logger.info("Headers created in Google Sheet.")

    return worksheet


# ─── Currency conversion ──────────────────────────────────────────────────────
def to_eur(amount: float, currency: str) -> Optional[float]:
    """Convert amount to EUR via Frankfurter API (free, no key required)."""
    currency = currency.upper()
    if currency == "EUR":
        return round(amount, 2)
    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={currency}&to=EUR",
            timeout=10,
        )
        r.raise_for_status()
        rate = r.json()["rates"]["EUR"]
        return round(amount * rate, 2)
    except Exception as exc:
        logger.warning("Currency API error for %s: %s", currency, exc)
        return None


# ─── UI helpers ───────────────────────────────────────────────────────────────
def get_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def build_category_keyboard() -> InlineKeyboardMarkup:
    """2-column inline keyboard with all expense categories."""
    keyboard = []
    for i in range(0, len(CATEGORIES), 2):
        row = [InlineKeyboardButton(CATEGORIES[i], callback_data=f"cat_{i}")]
        if i + 1 < len(CATEGORIES):
            row.append(
                InlineKeyboardButton(CATEGORIES[i + 1], callback_data=f"cat_{i + 1}")
            )
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def build_next_action_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard shown after each recorded expense."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Ще одна витрата", callback_data="next_add"),
                InlineKeyboardButton("✅ Завершити", callback_data="next_done"),
            ]
        ]
    )


# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привіт! Я бот для обліку витрат.*\n\n"
        "Всі дані зберігаються в Google Sheets.\n\n"
        "Натисніть *«➕ Додати витрату»* щоб почати.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: initialise session counter and show category picker."""
    if "expense_count" not in context.user_data:
        context.user_data["expense_count"] = 0

    await update.message.reply_text(
        "📂 *Оберіть категорію витрати:*",
        reply_markup=build_category_keyboard(),
        parse_mode="Markdown",
    )
    return CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split("_")[1])
    context.user_data["category"] = CATEGORIES[idx]

    await query.edit_message_text(
        f"✅ Категорія: *{CATEGORIES[idx]}*\n\n"
        "📝 Введіть *стаття* — короткий опис витрати.\n"
        "Або надішліть /skip, щоб стаття збіглася з категорією.",
        parse_mode="Markdown",
    )
    return ARTICLE


async def article_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["article"] = update.message.text.strip()
    return await _ask_amount(update, context)


async def article_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["article"] = context.user_data["category"]
    return await _ask_amount(update, context)


async def _ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["category"]
    art = context.user_data["article"]
    note = " _(дублює категорію)_" if art == cat else ""

    await update.message.reply_text(
        f"✅ Стаття: *{art}*{note}\n\n"
        "💰 Введіть *суму та валюту* через пробіл:\n"
        "`500 UAH`  ·  `50 EUR`  ·  `100 USD`  ·  `200 PLN`",
        parse_mode="Markdown",
    )
    return AMOUNT


async def amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()

    # ── Validate ──────────────────────────────────────────────────────────────
    if len(parts) != 2:
        await update.message.reply_text(
            "❌ Невірний формат.\n"
            "Введіть суму і валюту через пробіл, наприклад: `500 UAH`",
            parse_mode="Markdown",
        )
        return AMOUNT

    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "❌ Невірна сума. Спробуйте ще раз: `500 UAH`",
            parse_mode="Markdown",
        )
        return AMOUNT

    currency = parts[1].upper()

    # ── Convert to EUR ────────────────────────────────────────────────────────
    status_msg = await update.message.reply_text("⏳ Отримую курс валюти…")
    total_eur = to_eur(amount, currency)

    if total_eur is None:
        await status_msg.edit_text(
            f"⚠️ Не вдалося знайти курс для *{currency}*.\n"
            "Перевірте код валюти (UAH, USD, PLN, GBP…) та спробуйте знову.",
            parse_mode="Markdown",
        )
        return AMOUNT

    # ── Write to Google Sheets ────────────────────────────────────────────────
    user = update.effective_user
    date_str = datetime.now().strftime("%d.%m.%Y")
    name = get_display_name(user)
    category = context.user_data["category"]
    article = context.user_data["article"]

    row = [date_str, name, category, article, amount, currency, total_eur]

    try:
        sheet = get_sheet()
        sheet.append_row(row)

        # Increment session counter
        context.user_data["expense_count"] = context.user_data.get("expense_count", 0) + 1
        count = context.user_data["expense_count"]

        # Clear only current expense fields (keep counter)
        context.user_data.pop("category", None)
        context.user_data.pop("article", None)

        await status_msg.edit_text(
            f"✅ *Витрата #{count} записана!*\n\n"
            f"📅 *Дата:* {date_str}\n"
            f"👤 *ПІБ:* {name}\n"
            f"📂 *Категорія:* {category}\n"
            f"📝 *Стаття:* {article}\n"
            f"💰 *Сума:* {amount} {currency}\n"
            f"💶 *Разом:* {total_eur} EUR",
            parse_mode="Markdown",
        )

        # Offer next action
        await update.message.reply_text(
            "Що робимо далі?",
            reply_markup=build_next_action_keyboard(),
        )
        return NEXT_ACTION

    except Exception as exc:
        logger.error("Sheet write error: %s", exc)
        await status_msg.edit_text(
            "❌ *Помилка запису в Google Sheets.*\n"
            "Перевірте налаштування або зверніться до адміністратора.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END


async def handle_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Add another' or 'Done' after each expense."""
    query = update.callback_query
    await query.answer()

    if query.data == "next_add":
        # Remove the buttons and start a new expense right away
        await query.edit_message_text("Починаємо наступну витрату…")
        await query.message.reply_text(
            "📂 *Оберіть категорію витрати:*",
            reply_markup=build_category_keyboard(),
            parse_mode="Markdown",
        )
        return CATEGORY

    # Done
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()

    noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")
    await query.edit_message_text(
        f"✅ *Сесію завершено!*\n\nЗаписано {count} {noun}.\n\n"
        "Натисніть *«➕ Додати витрату»* коли знадобиться.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()

    text = "❌ Скасовано."
    if count:
        noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")
        text += f" У цій сесії записано {count} {noun}."

    await update.message.reply_text(
        text + "\n\nНатисніть *«➕ Додати витрату»* коли знадобиться.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            # Triggered by /add command OR the persistent bottom button
            CommandHandler("add", add_expense),
            MessageHandler(
                filters.Regex(r"^➕ Додати витрату$") & filters.TEXT,
                add_expense,
            ),
        ],
        states={
            CATEGORY: [
                CallbackQueryHandler(category_chosen, pattern=r"^cat_\d+$"),
            ],
            ARTICLE: [
                CommandHandler("skip", article_skipped),
                MessageHandler(filters.TEXT & ~filters.COMMAND, article_entered),
            ],
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_entered),
            ],
            NEXT_ACTION: [
                CallbackQueryHandler(handle_next_action, pattern=r"^next_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
