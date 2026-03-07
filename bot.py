#!/usr/bin/env python3
"""
Telegram Expense Tracker Bot v3
- Persistent bottom keyboard «➕ Додати витрату»
- Multi-expense sessions with session counter
- Auto EUR conversion via fawazahmed0 API (supports UAH, USD, PLN, GBP, CHF, CZK…)
  with Frankfurter as fallback
- ID column in Google Sheets for delete/edit
- Delete: inline button after entry OR /del ID
- Edit:   inline button after entry OR /edit ID
- Writes: Дата | ПІБ | Категорія | Стаття | Сума | Валюта | Разом (EUR) | ID
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
    "Проїзд",
    "Квартира",
    "Здоров'я",
    "Іграшки",
    "Подарунки",
    "Телефон, інтернет",
    "Одяг",
    "Навчання",
    "Відпочинок",
    "Обладнання",
    "Обіди на роботі",
    "Інше",
]

# Колонки: A–G = дані, H = ID
SHEET_HEADERS = [
    "Дата", "ПІБ", "Категорія", "Стаття", "Сума", "Валюта", "Разом (EUR)", "ID"
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
        raise ValueError("GOOGLE_CREDENTIALS_JSON is not set")

    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    client = gspread.authorize(creds)

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not set")

    ws = client.open_by_key(sheet_id).sheet1
    headers = ws.row_values(1)

    if not headers:
        # Brand-new sheet — create headers
        ws.append_row(SHEET_HEADERS)
        ws.format("A1:H1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.18, "green": 0.55, "blue": 0.34},
        })
        logger.info("Headers created in Google Sheet.")
    elif len(headers) < 8 or headers[7] != "ID":
        # Old 7-column sheet — add ID header in column H
        ws.update_cell(1, 8, "ID")
        logger.info("Added ID column header to existing sheet.")

    return ws


def next_expense_id(ws) -> int:
    """Return the next expense ID (= number of data rows currently in sheet)."""
    return len(ws.get_all_values())  # header + existing rows → new ID


def find_row_by_id(ws, expense_id: int) -> Optional[int]:
    """Return the 1-indexed row number for the given expense ID, or None."""
    try:
        col = ws.col_values(8)          # column H = ID
        for i, val in enumerate(col):
            if val == str(expense_id):
                return i + 1            # gspread is 1-indexed
    except Exception as exc:
        logger.warning("find_row_by_id error: %s", exc)
    return None


# ─── Currency conversion ──────────────────────────────────────────────────────
def to_eur(amount: float, currency: str) -> Optional[float]:
    """
    Convert *amount* in *currency* to EUR.
    Primary:  fawazahmed0 (free, no key, 170+ currencies incl. UAH)
    Fallback: Frankfurter (ECB rates)
    """
    currency = currency.upper()
    if currency == "EUR":
        return round(amount, 2)

    # ── Primary: fawazahmed0 ──────────────────────────────────────────────────
    try:
        url = (
            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest"
            f"/v1/currencies/{currency.lower()}.json"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        rate = r.json()[currency.lower()]["eur"]
        return round(amount * rate, 2)
    except Exception as exc:
        logger.warning("fawazahmed0 API error for %s: %s", currency, exc)

    # ── Fallback: Frankfurter (ECB) ───────────────────────────────────────────
    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={currency}&to=EUR",
            timeout=10,
        )
        r.raise_for_status()
        rate = r.json()["rates"]["EUR"]
        return round(amount * rate, 2)
    except Exception as exc:
        logger.warning("Frankfurter API error for %s: %s", currency, exc)

    return None


# ─── UI helpers ───────────────────────────────────────────────────────────────
def get_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def build_category_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    for i in range(0, len(CATEGORIES), 2):
        row = [InlineKeyboardButton(CATEGORIES[i], callback_data=f"cat_{i}")]
        if i + 1 < len(CATEGORIES):
            row.append(InlineKeyboardButton(CATEGORIES[i + 1], callback_data=f"cat_{i + 1}"))
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def build_next_action_keyboard(expense_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Ще одна витрата", callback_data="next_add"),
            InlineKeyboardButton("✅ Завершити",        callback_data="next_done"),
        ],
        [
            InlineKeyboardButton("✏️ Редагувати",          callback_data=f"edit_{expense_id}"),
            InlineKeyboardButton("🗑️ Видалити цей запис",  callback_data=f"del_{expense_id}"),
        ],
    ])


# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привіт! Я бот для обліку витрат.*\n\n"
        "Всі дані зберігаються в Google Sheets.\n\n"
        "*Команди:*\n"
        "/add — додати витрату\n"
        "/del ID — видалити запис  _(напр. /del 5)_\n"
        "/edit ID — редагувати запис  _(напр. /edit 5)_\n"
        "/cancel — скасувати поточну дію",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ─── Add expense ─────────────────────────────────────────────────────────────
async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "expense_count" not in context.user_data:
        context.user_data["expense_count"] = 0
    # Clear any leftover edit state
    for key in ("editing_id", "editing_row", "editing_original"):
        context.user_data.pop(key, None)

    await update.message.reply_text(
        "📂 *Оберіть категорію витрати:*",
        reply_markup=build_category_keyboard(),
        parse_mode="Markdown",
    )
    return CATEGORY


# ─── Category ────────────────────────────────────────────────────────────────
async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["category"] = category

    if category == "Інше":
        await query.edit_message_text(
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* — короткий опис витрати.\n"
            "⚠️ Для категорії «Інше» опис *обов'язковий*.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* — короткий опис витрати.\n"
            "Або /skip, щоб стаття збіглася з категорією.",
            parse_mode="Markdown",
        )
    return ARTICLE


# ─── Article ─────────────────────────────────────────────────────────────────
async def article_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["article"] = update.message.text.strip()
    return await _ask_amount(update, context)


async def article_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("category") == "Інше":
        await update.message.reply_text(
            "⚠️ Для категорії *«Інше»* стаття обов'язкова.\n"
            "Введіть короткий опис витрати.",
            parse_mode="Markdown",
        )
        return ARTICLE
    context.user_data["article"] = context.user_data["category"]
    return await _ask_amount(update, context)


async def _ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    art = context.user_data["article"]
    cat = context.user_data["category"]
    note = " _(дублює категорію)_" if art == cat else ""

    await update.message.reply_text(
        f"✅ Стаття: *{art}*{note}\n\n"
        "💰 Введіть *суму* та, за потреби, *валюту* через пробіл.\n"
        "Якщо валюту не вказати — буде EUR.\n\n"
        "*Приклади:*\n"
        "`500 UAH` — гривня 🇺🇦\n"
        "`50` або `50 EUR` — євро 🇪🇺\n"
        "`100 USD` — долар США 🇺🇸\n"
        "`200 PLN` — польський злотий 🇵🇱\n"
        "`150 GBP` — британський фунт 🇬🇧\n"
        "`120 CHF` — швейцарський франк 🇨🇭\n"
        "`80 CZK` — чеська крона 🇨🇿",
        parse_mode="Markdown",
    )
    return AMOUNT


# ─── Amount ───────────────────────────────────────────────────────────────────
async def amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()

    if not parts:
        await update.message.reply_text(
            "❌ Введіть суму, наприклад: `500 UAH` або просто `50`",
            parse_mode="Markdown",
        )
        return AMOUNT

    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "❌ Невірна сума. Спробуйте: `500 UAH` або `50`",
            parse_mode="Markdown",
        )
        return AMOUNT

    currency = parts[1].upper() if len(parts) >= 2 else "EUR"

    status_msg = await update.message.reply_text("⏳ Отримую курс валюти…")
    total_eur = to_eur(amount, currency)

    if total_eur is None:
        await status_msg.edit_text(
            f"⚠️ Не вдалося знайти курс для *{currency}*.\n"
            "Перевірте код валюти (UAH, USD, PLN, GBP…) та спробуйте знову.",
            parse_mode="Markdown",
        )
        return AMOUNT

    category = context.user_data["category"]
    article  = context.user_data["article"]
    is_edit  = "editing_id" in context.user_data

    try:
        ws = get_sheet()

        if is_edit:
            # ── Update existing row ───────────────────────────────────────────
            expense_id   = context.user_data["editing_id"]
            editing_row  = context.user_data["editing_row"]
            original     = context.user_data["editing_original"]
            date_str     = original[0]   # keep original date
            orig_name    = original[1]   # keep original author

            ws.update(
                range_name=f"A{editing_row}:H{editing_row}",
                values=[[date_str, orig_name, category, article,
                         amount, currency, total_eur, expense_id]],
            )
            for key in ("editing_id", "editing_row", "editing_original"):
                context.user_data.pop(key, None)

            await status_msg.edit_text(
                f"✏️ *Запис #{expense_id} оновлено!*\n\n"
                f"📅 *Дата:* {date_str}\n"
                f"👤 *ПІБ:* {orig_name}\n"
                f"📂 *Категорія:* {category}\n"
                f"📝 *Стаття:* {article}\n"
                f"💰 *Сума:* {amount} {currency}\n"
                f"💶 *Разом:* {total_eur} EUR",
                parse_mode="Markdown",
            )

        else:
            # ── Insert new row ────────────────────────────────────────────────
            date_str   = datetime.now().strftime("%d.%m.%Y")
            name       = get_display_name(update.effective_user)
            expense_id = next_expense_id(ws)

            ws.append_row([date_str, name, category, article,
                           amount, currency, total_eur, expense_id])

            context.user_data["expense_count"] = context.user_data.get("expense_count", 0) + 1
            count = context.user_data["expense_count"]
            context.user_data.pop("category", None)
            context.user_data.pop("article",  None)

            await status_msg.edit_text(
                f"✅ *Витрата #{count} записана!*  _(ID: {expense_id})_\n\n"
                f"📅 *Дата:* {date_str}\n"
                f"👤 *ПІБ:* {name}\n"
                f"📂 *Категорія:* {category}\n"
                f"📝 *Стаття:* {article}\n"
                f"💰 *Сума:* {amount} {currency}\n"
                f"💶 *Разом:* {total_eur} EUR",
                parse_mode="Markdown",
            )

        await update.message.reply_text(
            "Що робимо далі?",
            reply_markup=build_next_action_keyboard(expense_id),
        )
        return NEXT_ACTION

    except Exception as exc:
        logger.error("Sheet write/update error: %s", exc)
        await status_msg.edit_text(
            "❌ *Помилка запису в Google Sheets.*\n"
            "Перевірте налаштування або зверніться до адміністратора.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END


# ─── Next action (inline buttons after entry) ─────────────────────────────────
async def handle_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── ➕ Ще одна витрата ────────────────────────────────────────────────────
    if data == "next_add":
        await query.edit_message_text("Починаємо наступну витрату…")
        await query.message.reply_text(
            "📂 *Оберіть категорію витрати:*",
            reply_markup=build_category_keyboard(),
            parse_mode="Markdown",
        )
        return CATEGORY

    # ── 🗑️ Видалити ───────────────────────────────────────────────────────────
    if data.startswith("del_"):
        expense_id = int(data.split("_")[1])
        try:
            ws      = get_sheet()
            row_num = find_row_by_id(ws, expense_id)
            if row_num:
                ws.delete_rows(row_num)
                await query.edit_message_text(
                    f"🗑️ Запис *#{expense_id}* видалено.",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    f"⚠️ Запис *#{expense_id}* не знайдено.",
                    parse_mode="Markdown",
                )
        except Exception as exc:
            logger.error("Delete error: %s", exc)
            await query.edit_message_text("❌ Помилка видалення.")

        await query.message.reply_text(
            "Натисніть *«➕ Додати витрату»* коли знадобиться.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    # ── ✏️ Редагувати ─────────────────────────────────────────────────────────
    if data.startswith("edit_"):
        expense_id = int(data.split("_")[1])
        try:
            ws      = get_sheet()
            row_num = find_row_by_id(ws, expense_id)
            if not row_num:
                await query.edit_message_text(
                    f"⚠️ Запис *#{expense_id}* не знайдено.",
                    parse_mode="Markdown",
                )
                return NEXT_ACTION

            row = ws.row_values(row_num)
            context.user_data["editing_id"]       = expense_id
            context.user_data["editing_row"]      = row_num
            context.user_data["editing_original"] = row

            await query.edit_message_text(
                f"✏️ *Редагування запису #{expense_id}*\n\n"
                f"📅 {row[0]}  |  👤 {row[1]}\n"
                f"📂 {row[2]}  |  📝 {row[3]}\n"
                f"💰 {row[4]} {row[5]}  →  {row[6]} EUR\n\n"
                "Оберіть нову категорію:",
                parse_mode="Markdown",
            )
            await query.message.reply_text(
                "📂 *Оберіть категорію:*",
                reply_markup=build_category_keyboard(),
                parse_mode="Markdown",
            )
            return CATEGORY

        except Exception as exc:
            logger.error("Edit from button error: %s", exc)
            await query.edit_message_text(
                "❌ Помилка. Спробуйте `/edit ID`",
                parse_mode="Markdown",
            )
            return NEXT_ACTION

    # ── ✅ Завершити ──────────────────────────────────────────────────────────
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()
    noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")

    await query.edit_message_text(
        f"✅ *Сесію завершено!*\n\nЗаписано {count} {noun}.",
        parse_mode="Markdown",
    )
    await query.message.reply_text(
        "Натисніть *«➕ Додати витрату»* коли знадобиться.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─── /del ID ─────────────────────────────────────────────────────────────────
async def delete_expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Використання: `/del ID`\nНаприклад: `/del 5`",
            parse_mode="Markdown",
        )
        return

    expense_id = int(args[0])
    try:
        ws      = get_sheet()
        row_num = find_row_by_id(ws, expense_id)
        if row_num:
            ws.delete_rows(row_num)
            await update.message.reply_text(
                f"🗑️ Запис *#{expense_id}* видалено.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"⚠️ Запис *#{expense_id}* не знайдено.",
                parse_mode="Markdown",
            )
    except Exception as exc:
        logger.error("Delete cmd error: %s", exc)
        await update.message.reply_text("❌ Помилка видалення.")


# ─── /edit ID ────────────────────────────────────────────────────────────────
async def edit_expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Використання: `/edit ID`\nНаприклад: `/edit 5`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    expense_id = int(args[0])
    try:
        ws      = get_sheet()
        row_num = find_row_by_id(ws, expense_id)
        if not row_num:
            await update.message.reply_text(
                f"⚠️ Запис *#{expense_id}* не знайдено.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        row = ws.row_values(row_num)
        context.user_data["editing_id"]       = expense_id
        context.user_data["editing_row"]      = row_num
        context.user_data["editing_original"] = row
        context.user_data.setdefault("expense_count", 0)

        await update.message.reply_text(
            f"✏️ *Редагування запису #{expense_id}*\n\n"
            f"📅 {row[0]}  |  👤 {row[1]}\n"
            f"📂 {row[2]}  |  📝 {row[3]}\n"
            f"💰 {row[4]} {row[5]}  →  {row[6]} EUR\n\n"
            "Оберіть нову категорію або /cancel щоб скасувати:",
            parse_mode="Markdown",
            reply_markup=build_category_keyboard(),
        )
        return CATEGORY

    except Exception as exc:
        logger.error("Edit cmd error: %s", exc)
        await update.message.reply_text("❌ Помилка пошуку запису.")
        return ConversationHandler.END


# ─── /cancel ─────────────────────────────────────────────────────────────────
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


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("add",  add_expense),
            CommandHandler("edit", edit_expense_cmd),
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
                CallbackQueryHandler(
                    handle_next_action,
                    pattern=r"^(next_add|next_done|del_\d+|edit_\d+)$",
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("del",    delete_expense_cmd))
    app.add_handler(conv)

    logger.info("Bot v3 running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
