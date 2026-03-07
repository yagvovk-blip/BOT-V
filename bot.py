#!/usr/bin/env python3
"""
Telegram Expense Tracker Bot v4
────────────────────────────────────────────────────────────────────────────────
Головна клавіатура (persistent bottom):
  [➕ Додати витрату]
  [✏️ Змінити витрату]  [🗑️ Видалити витрату]

Зміна / видалення:
  • питає ID транзакції
  • показує поточні значення
  • дозволяє змінювати будь-які поля по черзі (і питає «що ще змінити?»)
  • Скасувати — на кожному кроці

Формат виводу транзакції (один рядок):
  📌 Категорія · Стаття · Сума Валюта
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
    ReplyKeyboardRemove,
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

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
# Add flow
ADD_CATEGORY, ADD_ARTICLE, ADD_AMOUNT, ADD_NEXT = range(4)
# Edit flow
EDIT_WAIT_ID, EDIT_CHOOSE_FIELD, EDIT_CAT, EDIT_ART, EDIT_AMT = range(4, 9)
# Delete flow
DEL_WAIT_ID, DEL_CONFIRM = range(9, 11)

# ─── Data ─────────────────────────────────────────────────────────────────────
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

SHEET_HEADERS = [
    "Дата", "ПІБ", "Категорія", "Стаття", "Сума", "Валюта", "Разом (EUR)", "ID"
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─── Keyboards ────────────────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["➕ Додати витрату"], ["✏️ Змінити витрату", "🗑️ Видалити витрату"]],
    resize_keyboard=True,
    is_persistent=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["❌ Скасувати"]],
    resize_keyboard=True,
)

# Фільтр для тексту кнопки «Скасувати»
CANCEL_TEXT = filters.Regex(r"^❌ Скасувати$") & filters.TEXT


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
        ws.append_row(SHEET_HEADERS)
        ws.format("A1:H1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.18, "green": 0.55, "blue": 0.34},
        })
        logger.info("Headers created.")
    elif len(headers) < 8 or headers[7] != "ID":
        ws.update_cell(1, 8, "ID")
        logger.info("Added ID column header.")

    return ws


def next_expense_id(ws) -> int:
    return len(ws.get_all_values())


def find_row_by_id(ws, expense_id: int) -> Optional[int]:
    try:
        col = ws.col_values(8)
        for i, val in enumerate(col):
            if val == str(expense_id):
                return i + 1
    except Exception as exc:
        logger.warning("find_row_by_id error: %s", exc)
    return None


def load_row(ws, expense_id: int):
    """Return (row_num, row_values) or (None, None)."""
    row_num = find_row_by_id(ws, expense_id)
    if row_num:
        return row_num, ws.row_values(row_num)
    return None, None


# ─── Currency ─────────────────────────────────────────────────────────────────
def to_eur(amount: float, currency: str) -> Optional[float]:
    """fawazahmed0 (primary, supports UAH) → Frankfurter (fallback)."""
    currency = currency.upper()
    if currency == "EUR":
        return round(amount, 2)

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
        logger.warning("fawazahmed0 error for %s: %s", currency, exc)

    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={currency}&to=EUR",
            timeout=10,
        )
        r.raise_for_status()
        rate = r.json()["rates"]["EUR"]
        return round(amount * rate, 2)
    except Exception as exc:
        logger.warning("Frankfurter error for %s: %s", currency, exc)

    return None


# ─── UI helpers ───────────────────────────────────────────────────────────────
def get_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def format_expense_line(category: str, article: str, amount, currency: str) -> str:
    """Одно-рядковий формат: 📌 Категорія · Стаття · 250 UAH"""
    parts = [category]
    if article and article != category:
        parts.append(article)
    parts.append(f"{amount} {currency}")
    return "📌 " + " · ".join(parts)


def build_category_keyboard(cancel_data: str = "cancel_conv") -> InlineKeyboardMarkup:
    keyboard = []
    for i in range(0, len(CATEGORIES), 2):
        row = [InlineKeyboardButton(CATEGORIES[i], callback_data=f"cat_{i}")]
        if i + 1 < len(CATEGORIES):
            row.append(InlineKeyboardButton(CATEGORIES[i + 1], callback_data=f"cat_{i + 1}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data=cancel_data)])
    return InlineKeyboardMarkup(keyboard)


def build_edit_field_keyboard() -> InlineKeyboardMarkup:
    """Клавіатура вибору поля для редагування — показує поточні значення."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Змінити категорію",    callback_data="ef_cat")],
        [InlineKeyboardButton("📝 Змінити стаття",       callback_data="ef_art")],
        [InlineKeyboardButton("💰 Змінити суму / валюту", callback_data="ef_amt")],
        [
            InlineKeyboardButton("✅ Зберегти",   callback_data="ef_save"),
            InlineKeyboardButton("❌ Скасувати", callback_data="ef_cancel"),
        ],
    ])


def build_delete_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Так, видалити", callback_data="del_yes"),
            InlineKeyboardButton("❌ Скасувати",     callback_data="del_no"),
        ],
    ])


def build_add_next_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Ще одна витрата", callback_data="next_add"),
            InlineKeyboardButton("✅ Завершити",        callback_data="next_done"),
        ],
    ])


# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привіт! Я бот для обліку витрат.*\n\n"
        "Використовуйте кнопки внизу:\n"
        "➕ *Додати витрату* — записати нову\n"
        "✏️ *Змінити витрату* — відредагувати за ID\n"
        "🗑️ *Видалити витрату* — видалити за ID\n\n"
        "На будь-якому кроці натисніть *❌ Скасувати* або /cancel.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ══════════════════════════════════════════════════════════════════════════════
# СПІЛЬНИЙ CANCEL
# ══════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()

    text = "❌ Скасовано."
    if count:
        noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")
        text += f" У цій сесії записано {count} {noun}."

    await update.message.reply_text(
        text + "\n\nНатисніть кнопку коли знадобиться.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ADD FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault("expense_count", 0)
    context.user_data.pop("category", None)
    context.user_data.pop("article", None)

    await update.message.reply_text(
        "📂 *Оберіть категорію витрати:*",
        reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
        parse_mode="Markdown",
    )
    return ADD_CATEGORY


async def add_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_conv":
        await query.edit_message_text("❌ Скасовано.")
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return ConversationHandler.END

    idx = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["category"] = category

    if category == "Інше":
        prompt = (
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* — короткий опис витрати.\n"
            "⚠️ Для категорії «Інше» опис *обов'язковий*."
        )
    else:
        prompt = (
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* — короткий опис витрати.\n"
            "Або /skip, щоб стаття збіглася з категорією."
        )

    await query.edit_message_text(prompt, parse_mode="Markdown")
    await query.message.reply_text(
        "Введіть текст або натисніть кнопку нижче:",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ADD_ARTICLE


async def add_article_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip() == "❌ Скасувати":
        return await cancel(update, context)
    context.user_data["article"] = update.message.text.strip()
    return await _add_ask_amount(update, context)


async def add_article_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("category") == "Інше":
        await update.message.reply_text(
            "⚠️ Для категорії *«Інше»* стаття обов'язкова. Введіть опис:",
            parse_mode="Markdown",
        )
        return ADD_ARTICLE
    context.user_data["article"] = context.user_data["category"]
    return await _add_ask_amount(update, context)


async def _add_ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        reply_markup=CANCEL_KEYBOARD,
    )
    return ADD_AMOUNT


async def add_amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        return await cancel(update, context)

    parts = text.split()
    try:
        amount = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Невірна сума. Спробуйте: `500 UAH` або `50`",
            parse_mode="Markdown",
        )
        return ADD_AMOUNT

    currency = parts[1].upper() if len(parts) >= 2 else "EUR"

    status_msg = await update.message.reply_text(
        "⏳ Отримую курс валюти…",
        reply_markup=CANCEL_KEYBOARD,
    )
    total_eur = to_eur(amount, currency)

    if total_eur is None:
        await status_msg.edit_text(
            f"⚠️ Не вдалося знайти курс для *{currency}*.\n"
            "Перевірте код валюти та спробуйте знову.",
            parse_mode="Markdown",
        )
        return ADD_AMOUNT

    try:
        ws         = get_sheet()
        date_str   = datetime.now().strftime("%d.%m.%Y")
        name       = get_display_name(update.effective_user)
        expense_id = next_expense_id(ws)
        category   = context.user_data["category"]
        article    = context.user_data["article"]

        ws.append_row([date_str, name, category, article, amount, currency, total_eur, expense_id])

        context.user_data["expense_count"] = context.user_data.get("expense_count", 0) + 1
        count = context.user_data["expense_count"]
        context.user_data.pop("category", None)
        context.user_data.pop("article", None)

        line = format_expense_line(category, article, amount, currency)

        await status_msg.edit_text(
            f"✅ *Витрата #{count} записана!*  _(ID: {expense_id})_\n\n{line}",
            parse_mode="Markdown",
        )
        await update.message.reply_text(
            "Що робимо далі?",
            reply_markup=build_add_next_keyboard(),
        )
        return ADD_NEXT

    except Exception as exc:
        logger.error("Sheet write error: %s", exc)
        await status_msg.edit_text("❌ *Помилка запису в Google Sheets.*", parse_mode="Markdown")
        context.user_data.clear()
        await update.message.reply_text("Спробуйте пізніше.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def add_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "next_add":
        await query.edit_message_text("Починаємо наступну витрату…")
        await query.message.reply_text(
            "📂 *Оберіть категорію витрати:*",
            reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
            parse_mode="Markdown",
        )
        return ADD_CATEGORY

    # next_done
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()
    noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")

    await query.edit_message_text(
        f"✅ *Сесію завершено!*  Записано {count} {noun}.",
        parse_mode="Markdown",
    )
    await query.message.reply_text(
        "Натисніть кнопку коли знадобиться.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_edit(context)
    await update.message.reply_text(
        "✏️ *Редагування витрати*\n\n"
        "Введіть *ID транзакції*, яку хочете змінити\n_(число з повідомлення про запис)_:",
        parse_mode="Markdown",
        reply_markup=CANCEL_KEYBOARD,
    )
    return EDIT_WAIT_ID


async def edit_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        return await cancel(update, context)

    if not text.isdigit():
        await update.message.reply_text(
            "❌ ID має бути числом. Спробуйте ще раз або натисніть *❌ Скасувати*.",
            parse_mode="Markdown",
        )
        return EDIT_WAIT_ID

    expense_id = int(text)
    try:
        ws = get_sheet()
        row_num, row = load_row(ws, expense_id)
        if not row_num:
            await update.message.reply_text(
                f"⚠️ Запис *#{expense_id}* не знайдено.\nВведіть інший ID або натисніть *❌ Скасувати*.",
                parse_mode="Markdown",
            )
            return EDIT_WAIT_ID

        # Збережемо поточні значення для редагування
        context.user_data.update({
            "edit_id":       expense_id,
            "edit_row_num":  row_num,
            "edit_date":     row[0] if len(row) > 0 else "",
            "edit_name":     row[1] if len(row) > 1 else "",
            "edit_category": row[2] if len(row) > 2 else "",
            "edit_article":  row[3] if len(row) > 3 else "",
            "edit_amount":   row[4] if len(row) > 4 else "",
            "edit_currency": row[5] if len(row) > 5 else "EUR",
        })

        line = format_expense_line(
            context.user_data["edit_category"],
            context.user_data["edit_article"],
            context.user_data["edit_amount"],
            context.user_data["edit_currency"],
        )
        await update.message.reply_text(
            f"✏️ *Редагування #{expense_id}*\n\n"
            f"Поточний запис:\n{line}\n\n"
            "Оберіть що змінити:",
            parse_mode="Markdown",
            reply_markup=build_edit_field_keyboard(),
        )
        return EDIT_CHOOSE_FIELD

    except Exception as exc:
        logger.error("edit_got_id error: %s", exc)
        await update.message.reply_text("❌ Помилка доступу до таблиці.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    ed   = context.user_data

    # ── Скасувати ─────────────────────────────────────────────────────────────
    if data == "ef_cancel":
        await query.edit_message_text("❌ Редагування скасовано.")
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        _clear_edit(context)
        return ConversationHandler.END

    # ── Зберегти ──────────────────────────────────────────────────────────────
    if data == "ef_save":
        await query.edit_message_text("⏳ Зберігаю зміни…")
        try:
            amount_val = float(str(ed["edit_amount"]).replace(",", "."))
            currency   = ed["edit_currency"].upper()
            total_eur  = to_eur(amount_val, currency)

            if total_eur is None:
                await query.message.reply_text(
                    f"⚠️ Не вдалося конвертувати *{currency}* в EUR.\n"
                    "Змініть суму або валюту і спробуйте ще раз.",
                    parse_mode="Markdown",
                    reply_markup=build_edit_field_keyboard(),
                )
                return EDIT_CHOOSE_FIELD

            ws = get_sheet()
            ws.update(
                range_name=f"A{ed['edit_row_num']}:H{ed['edit_row_num']}",
                values=[[
                    ed["edit_date"], ed["edit_name"],
                    ed["edit_category"], ed["edit_article"],
                    amount_val, currency, total_eur, ed["edit_id"],
                ]],
            )
            line = format_expense_line(ed["edit_category"], ed["edit_article"], amount_val, currency)
            await query.message.reply_text(
                f"✅ *Запис #{ed['edit_id']} оновлено!*\n\n{line}",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception as exc:
            logger.error("edit save error: %s", exc)
            await query.message.reply_text("❌ Помилка збереження.", reply_markup=MAIN_KEYBOARD)

        _clear_edit(context)
        return ConversationHandler.END

    # ── Змінити категорію ──────────────────────────────────────────────────────
    if data == "ef_cat":
        await query.edit_message_text(
            "📂 *Оберіть нову категорію:*",
            reply_markup=build_category_keyboard(cancel_data="efc_cancel"),
            parse_mode="Markdown",
        )
        return EDIT_CAT

    # ── Змінити стаття ────────────────────────────────────────────────────────
    if data == "ef_art":
        cat = ed.get("edit_category", "")
        hint = "⚠️ Для «Інше» стаття обов'язкова.\n" if cat == "Інше" else "Або надішліть /skip щоб стаття збіглася з категорією.\n"
        await query.edit_message_text(
            f"📝 *Введіть нову статтю:*\n{hint}",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Введіть або натисніть кнопку:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_ART

    # ── Змінити суму/валюту ───────────────────────────────────────────────────
    if data == "ef_amt":
        await query.edit_message_text(
            "💰 *Введіть нову суму та валюту:*\n\n"
            "*Приклади:*\n"
            "`500 UAH` — гривня 🇺🇦\n"
            "`50` або `50 EUR` — євро 🇪🇺\n"
            "`100 USD` — долар США 🇺🇸\n"
            "`200 PLN` — польський злотий 🇵🇱\n"
            "`150 GBP` — британський фунт 🇬🇧",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Введіть або натисніть кнопку:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_AMT

    return EDIT_CHOOSE_FIELD


async def edit_new_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробник вибору нової категорії під час редагування."""
    query = update.callback_query
    await query.answer()

    if query.data == "efc_cancel":
        # Повернутися до вибору полів без збереження зміни категорії
        await query.edit_message_text("↩️ Зміна категорії скасована.")
        return await _edit_show_fields(query.message, context)

    idx      = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["edit_category"] = category
    await query.edit_message_text(f"✅ Нова категорія: *{category}*", parse_mode="Markdown")
    return await _edit_show_fields(query.message, context)


async def edit_new_article(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробник введення нової статті під час редагування."""
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        await update.message.reply_text("↩️ Зміна статті скасована.")
        return await _edit_show_fields(update.message, context)

    if text == "/skip":
        category = context.user_data.get("edit_category", "")
        if category == "Інше":
            await update.message.reply_text(
                "⚠️ Для «Інше» стаття обов'язкова. Введіть опис:",
                reply_markup=CANCEL_KEYBOARD,
            )
            return EDIT_ART
        context.user_data["edit_article"] = category
        await update.message.reply_text(f"✅ Стаття: *{category}* _(дублює категорію)_", parse_mode="Markdown")
    else:
        context.user_data["edit_article"] = text
        await update.message.reply_text(f"✅ Нова стаття: *{text}*", parse_mode="Markdown")

    return await _edit_show_fields(update.message, context)


async def edit_new_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробник введення нової суми/валюти під час редагування."""
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        await update.message.reply_text("↩️ Зміна суми скасована.")
        return await _edit_show_fields(update.message, context)

    parts = text.split()
    try:
        amount = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Невірна сума. Спробуйте: `500 UAH` або `50`",
            parse_mode="Markdown",
        )
        return EDIT_AMT

    currency = parts[1].upper() if len(parts) >= 2 else "EUR"
    context.user_data["edit_amount"]   = amount
    context.user_data["edit_currency"] = currency
    await update.message.reply_text(f"✅ Нова сума: *{amount} {currency}*", parse_mode="Markdown")
    return await _edit_show_fields(update.message, context)


async def _edit_show_fields(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Надіслати клавіатуру вибору поля з поточними (можливо зміненими) значеннями."""
    ed = context.user_data
    line = format_expense_line(
        ed.get("edit_category", "—"),
        ed.get("edit_article",  "—"),
        ed.get("edit_amount",   "—"),
        ed.get("edit_currency", "EUR"),
    )
    await message.reply_text(
        f"✏️ *Запис #{ed.get('edit_id', '?')}*\n\n"
        f"Поточний стан:\n{line}\n\n"
        "Що ще змінити або збережіть:",
        parse_mode="Markdown",
        reply_markup=build_edit_field_keyboard(),
    )
    return EDIT_CHOOSE_FIELD


def _clear_edit(context: ContextTypes.DEFAULT_TYPE):
    for key in ("edit_id", "edit_row_num", "edit_date", "edit_name",
                "edit_category", "edit_article", "edit_amount", "edit_currency"):
        context.user_data.pop(key, None)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🗑️ *Видалення витрати*\n\n"
        "Введіть *ID транзакції*, яку хочете видалити\n_(число з повідомлення про запис)_:",
        parse_mode="Markdown",
        reply_markup=CANCEL_KEYBOARD,
    )
    return DEL_WAIT_ID


async def delete_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        return await cancel(update, context)

    if not text.isdigit():
        await update.message.reply_text(
            "❌ ID має бути числом. Спробуйте ще раз або натисніть *❌ Скасувати*.",
            parse_mode="Markdown",
        )
        return DEL_WAIT_ID

    expense_id = int(text)
    try:
        ws = get_sheet()
        row_num, row = load_row(ws, expense_id)
        if not row_num:
            await update.message.reply_text(
                f"⚠️ Запис *#{expense_id}* не знайдено.\nВведіть інший ID або натисніть *❌ Скасувати*.",
                parse_mode="Markdown",
            )
            return DEL_WAIT_ID

        context.user_data["del_id"]      = expense_id
        context.user_data["del_row_num"] = row_num

        line = format_expense_line(
            row[2] if len(row) > 2 else "",
            row[3] if len(row) > 3 else "",
            row[4] if len(row) > 4 else "",
            row[5] if len(row) > 5 else "EUR",
        )
        await update.message.reply_text(
            f"🗑️ *Видалення #{expense_id}*\n\n"
            f"{line}\n\n"
            "⚠️ Впевнені що хочете *видалити* цей запис?",
            parse_mode="Markdown",
            reply_markup=build_delete_confirm_keyboard(),
        )
        return DEL_CONFIRM

    except Exception as exc:
        logger.error("delete_got_id error: %s", exc)
        await update.message.reply_text("❌ Помилка доступу до таблиці.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "del_no":
        await query.edit_message_text("❌ Видалення скасовано.")
        context.user_data.pop("del_id", None)
        context.user_data.pop("del_row_num", None)
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    # del_yes
    expense_id = context.user_data.get("del_id")
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
                f"⚠️ Запис *#{expense_id}* вже не існує.",
                parse_mode="Markdown",
            )
    except Exception as exc:
        logger.error("delete_confirm error: %s", exc)
        await query.edit_message_text("❌ Помилка видалення.")

    context.user_data.pop("del_id", None)
    context.user_data.pop("del_row_num", None)
    await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    # ── Conversation: Add ──────────────────────────────────────────────────────
    add_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("add", add_expense),
            MessageHandler(filters.Regex(r"^➕ Додати витрату$") & filters.TEXT, add_expense),
        ],
        states={
            ADD_CATEGORY: [
                CallbackQueryHandler(add_category_chosen, pattern=r"^(cat_\d+|cancel_conv)$"),
            ],
            ADD_ARTICLE: [
                CommandHandler("skip", add_article_skipped),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_article_entered),
            ],
            ADD_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount_entered),
            ],
            ADD_NEXT: [
                CallbackQueryHandler(add_next_action, pattern=r"^(next_add|next_done)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(CANCEL_TEXT, cancel),
        ],
    )

    # ── Conversation: Edit ─────────────────────────────────────────────────────
    edit_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^✏️ Змінити витрату$") & filters.TEXT, edit_start),
        ],
        states={
            EDIT_WAIT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_got_id),
            ],
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(edit_choose_field, pattern=r"^ef_(cat|art|amt|save|cancel)$"),
            ],
            EDIT_CAT: [
                CallbackQueryHandler(edit_new_category, pattern=r"^(cat_\d+|efc_cancel)$"),
            ],
            EDIT_ART: [
                CommandHandler("skip", edit_new_article),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_article),
            ],
            EDIT_AMT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_amount),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(CANCEL_TEXT, cancel),
        ],
    )

    # ── Conversation: Delete ───────────────────────────────────────────────────
    del_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^🗑️ Видалити витрату$") & filters.TEXT, delete_start),
        ],
        states={
            DEL_WAIT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_got_id),
            ],
            DEL_CONFIRM: [
                CallbackQueryHandler(delete_confirm, pattern=r"^(del_yes|del_no)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(CANCEL_TEXT, cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(del_conv)

    logger.info("Bot v4 running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
