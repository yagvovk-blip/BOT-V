#!/usr/bin/env python3
"""
Telegram Expense Tracker Bot v4.4
────────────────────────────────────────────────────────────────────────────────
• MAIN_KEYBOARD завжди видима — CANCEL_KEYBOARD не використовується
• Сервісні повідомлення видаляються після запису суми (кінець add-flow)
• Результат edit/delete залишається в чаті
• Формат транзакції: _(ID: N)_  📌 Категорія · Стаття · 250 UAH
• Без "Натисніть кнопку коли знадобиться."
• Всі мережеві/Sheets виклики — asyncio.to_thread
"""

import asyncio
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
    Message,
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
ADD_CATEGORY, ADD_ARTICLE, ADD_AMOUNT, ADD_NEXT        = range(4)
EDIT_WAIT_ID, EDIT_CHOOSE_FIELD, EDIT_CAT, EDIT_ART, EDIT_AMT = range(4, 9)
DEL_WAIT_ID, DEL_CONFIRM                               = range(9, 11)

# ─── Data ─────────────────────────────────────────────────────────────────────
CATEGORIES = [
    "Продукти", "Заклади", "Проїзд", "Квартира", "Здоров'я",
    "Іграшки", "Подарунки", "Телефон, інтернет", "Одяг", "Навчання",
    "Відпочинок", "Обладнання", "Обіди на роботі", "Інше",
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

# Тексти кнопок головної клавіатури — щоб виключати їх із текстових хендлерів
_MAIN_BTN = filters.Regex(
    r"^(➕ Додати витрату|✏️ Змінити витрату|🗑️ Видалити витрату)$"
)
# Текстовий хендлер — тільки не-команди, не кнопки гол. меню
_TEXT = filters.TEXT & ~filters.COMMAND & ~_MAIN_BTN


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE-MESSAGE TRACKING (видалення сервісних повідомлень)
# ══════════════════════════════════════════════════════════════════════════════

def _track(context: ContextTypes.DEFAULT_TYPE, msg: Message) -> Message:
    context.user_data.setdefault("_del", []).append(msg)
    return msg


async def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    msgs = context.user_data.pop("_del", [])
    for msg in msgs:
        if msg is None:
            continue
        try:
            await msg.delete()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# SYNC HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON is not set")
    creds  = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not set")
    ws      = client.open_by_key(sheet_id).sheet1
    headers = ws.row_values(1)
    if not headers:
        ws.append_row(SHEET_HEADERS)
        ws.format("A1:H1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.18, "green": 0.55, "blue": 0.34},
        })
    elif len(headers) < 8 or headers[7] != "ID":
        ws.update_cell(1, 8, "ID")

    # Формула накопичувальної суми за поточний місяць у клітинці J1 (встановлюється один раз)
    try:
        j1_val = ws.acell("J1").value
        if not j1_val or not str(j1_val).strip():
            formula = (
                "=SUMPRODUCT("
                "(MONTH(DATEVALUE(IF(A2:A10000<>\"\",A2:A10000,\"1.1.2000\")))=MONTH(TODAY()))*"
                "(YEAR(DATEVALUE(IF(A2:A10000<>\"\",A2:A10000,\"1.1.2000\")))=YEAR(TODAY()))*"
                "(ISNUMBER(G2:G10000))*(G2:G10000))"
            )
            ws.update_acell("J1", formula)
    except Exception as exc:
        logger.warning("J1 formula setup error: %s", exc)

    return ws


def _next_id(ws) -> int:
    return len(ws.get_all_values())


def _find_row(ws, expense_id: int) -> Optional[int]:
    try:
        col = ws.col_values(8)
        for i, val in enumerate(col):
            if val == str(expense_id):
                return i + 1
    except Exception as exc:
        logger.warning("_find_row error: %s", exc)
    return None


def _load_row(ws, expense_id: int):
    row_num = _find_row(ws, expense_id)
    if row_num:
        return row_num, ws.row_values(row_num)
    return None, None


def _to_eur(amount: float, currency: str) -> Optional[float]:
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


# ── Async wrappers ─────────────────────────────────────────────────────────────

async def get_sheet():
    return await asyncio.to_thread(_get_sheet)

async def to_eur(amount: float, currency: str) -> Optional[float]:
    return await asyncio.to_thread(_to_eur, amount, currency)

async def sheet_append(ws, row: list):
    await asyncio.to_thread(ws.append_row, row)

async def sheet_update(ws, range_name: str, values: list):
    await asyncio.to_thread(lambda: ws.update(range_name=range_name, values=values))

async def sheet_delete_row(ws, row_num: int):
    await asyncio.to_thread(ws.delete_rows, row_num)


def _monthly_total_sync(ws) -> str:
    """Читає накопичувальну суму за поточний місяць з клітинки J1 (формула в Sheets)."""
    try:
        val = ws.acell("J1").value
        if val is not None and str(val).strip():
            return f"{float(str(val).replace(',', '.')):.2f}"
    except Exception as exc:
        logger.warning("monthly_total read error: %s", exc)
    return "—"


async def monthly_total(ws) -> str:
    return await asyncio.to_thread(_monthly_total_sync, ws)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_display_name(user) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def _expense_parts(category: str, article: str, amount, currency: str) -> str:
    """Повертає рядок без ID: 'Категорія · Стаття · 250 UAH'"""
    parts = [category]
    if article and article != category:
        parts.append(article)
    parts.append(f"{amount} {currency}")
    return " · ".join(parts)


def fmt(expense_id, category: str, article: str, amount, currency: str) -> str:
    """Markdown: _(ID: N)_  📌 Категорія · Стаття · 250 UAH"""
    return f"_(ID: {expense_id})_  📌 " + _expense_parts(category, article, amount, currency)


def fmt_edited(expense_id, category: str, article: str, amount, currency: str) -> str:
    """Markdown: ✏️ _(ID: N)_  📌 Категорія · Стаття · 250 UAH"""
    return f"✏️ _(ID: {expense_id})_  📌 " + _expense_parts(category, article, amount, currency)


def fmt_deleted_html(expense_id, category: str, article: str, amount, currency: str) -> str:
    """HTML: 🗑️ <i>(ID: N)</i>  <s>📌 Категорія · Стаття · 250 UAH</s>"""
    line = _expense_parts(category, article, amount, currency)
    return f"🗑️ <i>(ID: {expense_id})</i>  <s>📌 {line}</s>"


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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Категорія",      callback_data="ef_cat")],
        [InlineKeyboardButton("📝 Стаття",         callback_data="ef_art")],
        [InlineKeyboardButton("💰 Сума / валюта",  callback_data="ef_amt")],
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
        "➕ *Додати витрату* — записати нову\n"
        "✏️ *Змінити витрату* — відредагувати за ID\n"
        "🗑️ *Видалити витрату* — видалити за ID\n\n"
        "На будь-якому кроці /cancel для скасування.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ─── Спільний cancel ──────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup(context)
    context.user_data.clear()
    await update.message.reply_text("❌ Скасовано.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ADD FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_del"] = []
    context.user_data.setdefault("expense_count", 0)
    context.user_data.pop("category", None)
    context.user_data.pop("article", None)

    _track(context, update.message)  # кнопка "➕ Додати витрату"
    msg = await update.message.reply_text(
        "📂 *Оберіть категорію:*",
        reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
        parse_mode="Markdown",
    )
    _track(context, msg)
    return ADD_CATEGORY


async def add_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_conv":
        await _cleanup(context)
        context.user_data.clear()
        await context.bot.send_message(
            query.message.chat_id, "❌ Скасовано.", reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END

    idx      = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["category"] = category

    if category == "Інше":
        prompt = (
            f"✅ *{category}*\n\n"
            "📝 Введіть *стаття* — опис витрати.\n"
            "⚠️ Для «Інше» обов'язково."
        )
    else:
        prompt = (
            f"✅ *{category}*\n\n"
            "📝 Введіть *стаття* або /skip щоб дублювати категорію:"
        )
    await query.edit_message_text(prompt, parse_mode="Markdown")
    return ADD_ARTICLE


async def add_article_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    context.user_data["article"] = update.message.text.strip()
    return await _add_ask_amount(update, context)


async def add_article_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    if context.user_data.get("category") == "Інше":
        msg = await update.message.reply_text(
            "⚠️ Для «Інше» стаття обов'язкова. Введіть опис:",
            parse_mode="Markdown",
        )
        _track(context, msg)
        return ADD_ARTICLE
    context.user_data["article"] = context.user_data["category"]
    return await _add_ask_amount(update, context)


async def _add_ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    art  = context.user_data["article"]
    cat  = context.user_data["category"]
    note = " _(= категорія)_" if art == cat else ""

    # Редагуємо повідомлення з категорією, щоб показати крок суми
    # (шукаємо його в _del — це другий елемент)
    cat_msg = None
    for m in context.user_data.get("_del", []):
        if hasattr(m, "reply_markup") and m.reply_markup:
            cat_msg = m
            break

    amount_prompt = (
        f"✅ *{art}*{note}\n\n"
        "💰 Введіть *суму* та, за потреби, *валюту* через пробіл.\n"
        "Без валюти — буде EUR.\n\n"
        "`500 UAH` · `50 EUR` · `100 USD` · `200 PLN`\n"
        "`150 GBP` · `120 CHF` · `80 CZK`"
    )

    if cat_msg:
        try:
            await cat_msg.edit_text(amount_prompt, parse_mode="Markdown")
            return ADD_AMOUNT
        except Exception:
            pass

    msg = await update.message.reply_text(amount_prompt, parse_mode="Markdown")
    _track(context, msg)
    return ADD_AMOUNT


async def add_amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)

    parts = update.message.text.strip().split()
    try:
        amount = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        msg = await update.message.reply_text(
            "❌ Невірна сума. Спробуйте: `500 UAH` або `50`",
            parse_mode="Markdown",
        )
        _track(context, msg)
        return ADD_AMOUNT

    currency   = parts[1].upper() if len(parts) >= 2 else "EUR"
    status_msg = await update.message.reply_text("⏳ Отримую курс…")
    _track(context, status_msg)

    total_eur = await to_eur(amount, currency)
    if total_eur is None:
        await status_msg.edit_text(
            f"⚠️ Не вдалося знайти курс для *{currency}*.\n"
            "Перевірте код валюти та спробуйте знову.",
            parse_mode="Markdown",
        )
        return ADD_AMOUNT

    try:
        category   = context.user_data["category"]
        article    = context.user_data["article"]
        date_str   = datetime.now().strftime("%d.%m.%Y")
        name       = get_display_name(update.effective_user)
        ws         = await get_sheet()
        expense_id = await asyncio.to_thread(_next_id, ws)

        await sheet_append(ws, [date_str, name, category, article, amount, currency, total_eur, expense_id])

        context.user_data["expense_count"] = context.user_data.get("expense_count", 0) + 1
        context.user_data.pop("category", None)
        context.user_data.pop("article", None)

        # Видаляємо ВСІ сервісні повідомлення після успішного запису
        await _cleanup(context)

        # Накопичувальна сума за поточний місяць
        month_str = await monthly_total(ws)

        # Єдиний результат — залишається в чаті
        await update.message.reply_text(
            fmt(expense_id, category, article, amount, currency)
            + f"\n📊 За місяць: {month_str} EUR",
            parse_mode="Markdown",
            reply_markup=build_add_next_keyboard(),
        )
        return ADD_NEXT

    except Exception as exc:
        logger.error("Sheet write error: %s", exc)
        await _cleanup(context)
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Помилка запису в Google Sheets. Спробуйте пізніше.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END


async def add_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "next_add":
        # Прибираємо inline-кнопки з повідомлення транзакції (залишається в чаті)
        await query.edit_message_reply_markup(reply_markup=None)
        # Починаємо нову витрату
        context.user_data["_del"] = []
        msg = await query.message.reply_text(
            "📂 *Оберіть категорію:*",
            reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
            parse_mode="Markdown",
        )
        _track(context, msg)
        return ADD_CATEGORY

    # next_done — просто прибираємо кнопки, MAIN_KEYBOARD вже є
    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_edit(context)
    context.user_data["_del"] = []

    _track(context, update.message)
    msg = await update.message.reply_text(
        "✏️ *Редагування*\n\nВведіть *ID транзакції:*",
        parse_mode="Markdown",
    )
    _track(context, msg)
    return EDIT_WAIT_ID


async def edit_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if not text.isdigit():
        msg = await update.message.reply_text(
            "❌ ID має бути числом. Спробуйте ще раз.",
        )
        _track(context, msg)
        return EDIT_WAIT_ID

    expense_id = int(text)
    wait_msg   = await update.message.reply_text("⏳")
    _track(context, wait_msg)

    try:
        ws = await get_sheet()
        row_num, row = await asyncio.to_thread(_load_row, ws, expense_id)

        if not row_num:
            await wait_msg.edit_text(f"⚠️ Запис *#{expense_id}* не знайдено.", parse_mode="Markdown")
            return EDIT_WAIT_ID

        context.user_data.update({
            "edit_id":       expense_id,
            "edit_row_num":  row_num,
            "edit_ws":       ws,
            "edit_date":     row[0] if len(row) > 0 else "",
            "edit_name":     row[1] if len(row) > 1 else "",
            "edit_category": row[2] if len(row) > 2 else "",
            "edit_article":  row[3] if len(row) > 3 else "",
            "edit_amount":   row[4] if len(row) > 4 else "0",
            "edit_currency": row[5] if len(row) > 5 else "EUR",
        })

        line = fmt(
            expense_id,
            context.user_data["edit_category"],
            context.user_data["edit_article"],
            context.user_data["edit_amount"],
            context.user_data["edit_currency"],
        )
        # Видаляємо сервісні (кнопка, запит ID, введений ID, ⏳)
        await _cleanup(context)

        msg = await update.message.reply_text(
            f"{line}\n\nЩо змінити?",
            parse_mode="Markdown",
            reply_markup=build_edit_field_keyboard(),
        )
        _track(context, msg)
        return EDIT_CHOOSE_FIELD

    except Exception as exc:
        logger.error("edit_got_id error: %s", exc)
        await _cleanup(context)
        await update.message.reply_text("❌ Помилка доступу до таблиці.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    ed   = context.user_data

    # ── Скасувати ─────────────────────────────────────────────────────────────
    if data == "ef_cancel":
        chat_id = query.message.chat_id
        await _cleanup(context)
        _clear_edit(context)
        await context.bot.send_message(chat_id, "❌ Редагування скасовано.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    # ── Зберегти ──────────────────────────────────────────────────────────────
    if data == "ef_save":
        chat_id = query.message.chat_id
        try:
            amount_val = float(str(ed["edit_amount"]).replace(",", "."))
            currency   = ed["edit_currency"].upper()
            total_eur  = await to_eur(amount_val, currency)

            if total_eur is None:
                await query.edit_message_text(
                    f"⚠️ Не вдалося конвертувати *{currency}* в EUR.\nЗмініть суму або валюту.",
                    parse_mode="Markdown",
                    reply_markup=build_edit_field_keyboard(),
                )
                return EDIT_CHOOSE_FIELD

            ws      = ed.get("edit_ws") or await get_sheet()
            row_num = ed["edit_row_num"]
            exp_id  = ed["edit_id"]

            await sheet_update(
                ws,
                range_name=f"A{row_num}:H{row_num}",
                values=[[
                    ed["edit_date"], ed["edit_name"],
                    ed["edit_category"], ed["edit_article"],
                    amount_val, currency, total_eur, exp_id,
                ]],
            )
            line = fmt_edited(exp_id, ed["edit_category"], ed["edit_article"], amount_val, currency)

            # Накопичувальна сума за поточний місяць (після оновлення)
            month_str = await monthly_total(ws)

            # Видаляємо сервісні (включно з полем вибору)
            await _cleanup(context)
            _clear_edit(context)

            # Результат залишається в чаті + відновлює MAIN_KEYBOARD
            await context.bot.send_message(
                chat_id,
                line + f"\n📊 За місяць: {month_str} EUR",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception as exc:
            logger.error("edit save error: %s", exc)
            await _cleanup(context)
            _clear_edit(context)
            await context.bot.send_message(chat_id, "❌ Помилка збереження.", reply_markup=MAIN_KEYBOARD)

        return ConversationHandler.END

    # ── Вибір поля ────────────────────────────────────────────────────────────
    if data == "ef_cat":
        await query.edit_message_text(
            "📂 *Оберіть нову категорію:*",
            reply_markup=build_category_keyboard(cancel_data="efc_cancel"),
            parse_mode="Markdown",
        )
        return EDIT_CAT

    if data == "ef_art":
        cat  = ed.get("edit_category", "")
        hint = "⚠️ Для «Інше» обов'язково.\n" if cat == "Інше" else "Або /skip щоб = категорія.\n"
        await query.edit_message_text(
            f"📝 *Нова стаття:*\n{hint}",
            parse_mode="Markdown",
        )
        return EDIT_ART

    if data == "ef_amt":
        await query.edit_message_text(
            "💰 *Нова сума та валюта:*\n\n"
            "`500 UAH` · `50 EUR` · `100 USD` · `200 PLN` · `150 GBP`",
            parse_mode="Markdown",
        )
        return EDIT_AMT

    return EDIT_CHOOSE_FIELD


async def edit_new_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data != "efc_cancel":
        idx      = int(query.data.split("_")[1])
        context.user_data["edit_category"] = CATEGORIES[idx]

    return await _edit_show_fields_cb(query, context)


async def edit_new_article(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if text != "/skip":
        context.user_data["edit_article"] = text
    else:
        category = context.user_data.get("edit_category", "")
        if category == "Інше":
            msg = await update.message.reply_text(
                "⚠️ Для «Інше» стаття обов'язкова. Введіть опис:",
            )
            _track(context, msg)
            return EDIT_ART
        context.user_data["edit_article"] = category

    await _cleanup(context)
    return await _edit_show_fields_msg(update.message, context)


async def edit_new_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text  = update.message.text.strip()
    parts = text.split()
    try:
        amount = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        msg = await update.message.reply_text(
            "❌ Невірна сума. Спробуйте: `500 UAH` або `50`",
            parse_mode="Markdown",
        )
        _track(context, msg)
        return EDIT_AMT

    currency = parts[1].upper() if len(parts) >= 2 else "EUR"
    context.user_data["edit_amount"]   = amount
    context.user_data["edit_currency"] = currency

    await _cleanup(context)
    return await _edit_show_fields_msg(update.message, context)


async def _edit_show_fields_cb(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Оновити поточне повідомлення з вибором полів (після inline-дії)."""
    ed   = context.user_data
    line = fmt(
        ed.get("edit_id", "?"),
        ed.get("edit_category", "—"),
        ed.get("edit_article",  "—"),
        ed.get("edit_amount",   "—"),
        ed.get("edit_currency", "EUR"),
    )
    await query.edit_message_text(
        f"{line}\n\nЩо ще змінити або збережіть:",
        parse_mode="Markdown",
        reply_markup=build_edit_field_keyboard(),
    )
    return EDIT_CHOOSE_FIELD


async def _edit_show_fields_msg(message: Message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Надіслати нове повідомлення з вибором полів (після текстового введення)."""
    ed   = context.user_data
    line = fmt(
        ed.get("edit_id", "?"),
        ed.get("edit_category", "—"),
        ed.get("edit_article",  "—"),
        ed.get("edit_amount",   "—"),
        ed.get("edit_currency", "EUR"),
    )
    msg = await message.reply_text(
        f"{line}\n\nЩо ще змінити або збережіть:",
        parse_mode="Markdown",
        reply_markup=build_edit_field_keyboard(),
    )
    _track(context, msg)
    return EDIT_CHOOSE_FIELD


def _clear_edit(context: ContextTypes.DEFAULT_TYPE):
    for key in ("edit_id", "edit_row_num", "edit_ws", "edit_date", "edit_name",
                "edit_category", "edit_article", "edit_amount", "edit_currency"):
        context.user_data.pop(key, None)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_del"] = []
    _track(context, update.message)

    msg = await update.message.reply_text(
        "🗑️ *Видалення*\n\nВведіть *ID транзакції:*",
        parse_mode="Markdown",
    )
    _track(context, msg)
    return DEL_WAIT_ID


async def delete_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if not text.isdigit():
        msg = await update.message.reply_text("❌ ID має бути числом. Спробуйте ще раз.")
        _track(context, msg)
        return DEL_WAIT_ID

    expense_id = int(text)
    wait_msg   = await update.message.reply_text("⏳")
    _track(context, wait_msg)

    try:
        ws = await get_sheet()
        row_num, row = await asyncio.to_thread(_load_row, ws, expense_id)

        if not row_num:
            await wait_msg.edit_text(f"⚠️ Запис *#{expense_id}* не знайдено.", parse_mode="Markdown")
            return DEL_WAIT_ID

        context.user_data["del_id"]  = expense_id
        context.user_data["del_ws"]  = ws
        # Зберігаємо дані рядка для зачеркнутого результату
        context.user_data["del_row"] = row

        line = fmt(
            expense_id,
            row[2] if len(row) > 2 else "",
            row[3] if len(row) > 3 else "",
            row[4] if len(row) > 4 else "",
            row[5] if len(row) > 5 else "EUR",
        )

        await _cleanup(context)

        msg = await update.message.reply_text(
            f"{line}\n\n⚠️ Видалити цей запис?",
            parse_mode="Markdown",
            reply_markup=build_delete_confirm_keyboard(),
        )
        _track(context, msg)
        return DEL_CONFIRM

    except Exception as exc:
        logger.error("delete_got_id error: %s", exc)
        await _cleanup(context)
        await update.message.reply_text("❌ Помилка доступу до таблиці.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id    = query.message.chat_id
    expense_id = context.user_data.get("del_id")
    ws         = context.user_data.get("del_ws")

    if query.data == "del_no":
        await _cleanup(context)
        context.user_data.pop("del_id", None)
        context.user_data.pop("del_ws", None)
        await context.bot.send_message(chat_id, "❌ Видалення скасовано.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    # del_yes
    row        = context.user_data.get("del_row", [])
    parse_mode = "HTML"
    try:
        if not ws:
            ws = await get_sheet()
        row_num = await asyncio.to_thread(_find_row, ws, expense_id)
        if row_num:
            await sheet_delete_row(ws, row_num)
            month_str = await monthly_total(ws)
            result_text = (
                fmt_deleted_html(
                    expense_id,
                    row[2] if len(row) > 2 else "",
                    row[3] if len(row) > 3 else "",
                    row[4] if len(row) > 4 else "",
                    row[5] if len(row) > 5 else "EUR",
                )
                + f"\n📊 За місяць: {month_str} EUR"
            )
        else:
            result_text = f"⚠️ <i>(ID: {expense_id})</i> вже не існує"
    except Exception as exc:
        logger.error("delete_confirm error: %s", exc)
        result_text = "❌ Помилка видалення"
        parse_mode  = None

    # Видаляємо підтвердження, надсилаємо результат (залишається в чаті)
    await _cleanup(context)
    context.user_data.pop("del_id",  None)
    context.user_data.pop("del_ws",  None)
    context.user_data.pop("del_row", None)

    await context.bot.send_message(
        chat_id,
        result_text,
        parse_mode=parse_mode,
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    _fallbacks = [
        CommandHandler("cancel", cancel),
        MessageHandler(_MAIN_BTN, cancel),  # натискання кнопок гол. меню скасовує поточну дію
    ]

    add_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("add", add_expense),
            MessageHandler(filters.Regex(r"^➕ Додати витрату$") & filters.TEXT, add_expense),
        ],
        states={
            ADD_CATEGORY: [CallbackQueryHandler(add_category_chosen, pattern=r"^(cat_\d+|cancel_conv)$")],
            ADD_ARTICLE:  [
                CommandHandler("skip", add_article_skipped),
                MessageHandler(_TEXT, add_article_entered),
            ],
            ADD_AMOUNT: [MessageHandler(_TEXT, add_amount_entered)],
            ADD_NEXT:   [CallbackQueryHandler(add_next_action, pattern=r"^(next_add|next_done)$")],
        },
        fallbacks=_fallbacks,
    )

    edit_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^✏️ Змінити витрату$") & filters.TEXT, edit_start),
        ],
        states={
            EDIT_WAIT_ID:      [MessageHandler(_TEXT, edit_got_id)],
            EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_choose_field, pattern=r"^ef_(cat|art|amt|save|cancel)$")],
            EDIT_CAT:          [CallbackQueryHandler(edit_new_category, pattern=r"^(cat_\d+|efc_cancel)$")],
            EDIT_ART:          [
                CommandHandler("skip", edit_new_article),
                MessageHandler(_TEXT, edit_new_article),
            ],
            EDIT_AMT: [MessageHandler(_TEXT, edit_new_amount)],
        },
        fallbacks=_fallbacks,
    )

    del_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^🗑️ Видалити витрату$") & filters.TEXT, delete_start),
        ],
        states={
            DEL_WAIT_ID: [MessageHandler(_TEXT, delete_got_id)],
            DEL_CONFIRM: [CallbackQueryHandler(delete_confirm, pattern=r"^(del_yes|del_no)$")],
        },
        fallbacks=_fallbacks,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(del_conv)

    logger.info("Bot v4.4 running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
