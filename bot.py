#!/usr/bin/env python3
"""
Telegram Expense Tracker Bot v4.2
────────────────────────────────────────────────────────────────────────────────
• Всі мережеві/Sheets виклики — asyncio.to_thread (event loop не блокується)
• Сервісні повідомлення видаляються; в чаті залишається лише підсумок
  транзакції / редагування / видалення
• Головна клавіатура:
    [➕ Додати витрату]
    [✏️ Змінити витрату]  [🗑️ Видалити витрату]
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

# ─── Logging ──────────────────────────────────────────────────────────────────
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
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["❌ Скасувати"]],
    resize_keyboard=True,
)
CANCEL_TEXT = filters.Regex(r"^❌ Скасувати$") & filters.TEXT


# ══════════════════════════════════════════════════════════════════════════════
# УТИЛІТИ: видалення повідомлень
# ══════════════════════════════════════════════════════════════════════════════

async def _delete_msgs(msgs: list):
    """Видаляє список повідомлень (ігнорує помилки, якщо вже видалено)."""
    for msg in msgs:
        if msg is None:
            continue
        try:
            await msg.delete()
        except Exception:
            pass


def _track(context: ContextTypes.DEFAULT_TYPE, msg: Message):
    """Запам'ятати повідомлення для подальшого видалення."""
    context.user_data.setdefault("_to_delete", []).append(msg)
    return msg


async def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    """Видалити всі накопичені сервісні повідомлення."""
    await _delete_msgs(context.user_data.pop("_to_delete", []))


# ══════════════════════════════════════════════════════════════════════════════
# SYNC HELPERS  (виконуються у потоці через asyncio.to_thread)
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
    """fawazahmed0 primary (підтримує UAH), Frankfurter fallback."""
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


# ─── UI helpers ───────────────────────────────────────────────────────────────

def get_display_name(user) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def format_expense_line(category: str, article: str, amount, currency: str) -> str:
    """📌 Категорія · Стаття · 250 UAH"""
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Змінити категорію",     callback_data="ef_cat")],
        [InlineKeyboardButton("📝 Змінити стаття",        callback_data="ef_art")],
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


# ─── Спільний cancel ──────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    count = context.user_data.get("expense_count", 0)
    await _cleanup(context)
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
    context.user_data["_to_delete"] = []

    # Повідомлення з вибором категорії — сервісне, треба видалити потім
    msg = await update.message.reply_text(
        "📂 *Оберіть категорію витрати:*",
        reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
        parse_mode="Markdown",
    )
    _track(context, msg)
    # Повідомлення юзера теж видаляємо (кнопка «Додати витрату»)
    _track(context, update.message)
    return ADD_CATEGORY


async def add_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_conv":
        await _cleanup(context)
        await query.message.delete()
        context.user_data.clear()
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    idx      = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["category"] = category

    # Замінюємо повідомлення з категоріями — стаємо на крок статті
    if category == "Інше":
        prompt = (
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* — короткий опис витрати.\n"
            "⚠️ Для «Інше» опис *обов'язковий*."
        )
    else:
        prompt = (
            f"✅ Категорія: *{category}*\n\n"
            "📝 Введіть *стаття* або /skip щоб дублювати категорію:"
        )
    await query.edit_message_text(prompt, parse_mode="Markdown")

    # Окреме повідомлення з CANCEL_KEYBOARD — сервісне
    msg = await query.message.reply_text("↓", reply_markup=CANCEL_KEYBOARD)
    _track(context, msg)
    return ADD_ARTICLE


async def add_article_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    if update.message.text.strip() == "❌ Скасувати":
        return await cancel(update, context)
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
    note = " _(дублює категорію)_" if art == cat else ""

    # Оновлюємо останнє інлайн-повідомлення (з вибором категорії)
    # Воно вже є в _to_delete; але редагуємо через send нового
    msg = await update.message.reply_text(
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
    _track(context, msg)
    return ADD_AMOUNT


async def add_amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)

    if update.message.text.strip() == "❌ Скасувати":
        return await cancel(update, context)

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
    status_msg = await update.message.reply_text("⏳ Отримую курс валюти…")
    _track(context, status_msg)

    # Конвертація (не блокує event loop)
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
        count = context.user_data["expense_count"]
        context.user_data.pop("category", None)
        context.user_data.pop("article", None)

        # Видаляємо всі сервісні повідомлення
        await _cleanup(context)

        line = format_expense_line(category, article, amount, currency)
        # Єдине підсумкове повідомлення — залишається в чаті
        await update.message.reply_text(
            f"✅ *Витрата #{count} записана!*  _(ID: {expense_id})_\n\n{line}",
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
        context.user_data["_to_delete"] = []
        await query.edit_message_text(
            f"{query.message.text}\n\n_Додаємо наступну…_",
            parse_mode="Markdown",
        )
        msg = await query.message.reply_text(
            "📂 *Оберіть категорію витрати:*",
            reply_markup=build_category_keyboard(cancel_data="cancel_conv"),
            parse_mode="Markdown",
        )
        _track(context, msg)
        return ADD_CATEGORY

    # next_done — підсумок сесії
    count = context.user_data.get("expense_count", 0)
    context.user_data.clear()
    noun = "витрату" if count == 1 else ("витрати" if count in (2, 3, 4) else "витрат")
    await query.edit_message_text(
        f"{query.message.text}\n\n✅ _Сесію завершено. Разом: {count} {noun}._",
        parse_mode="Markdown",
        reply_markup=None,
    )
    await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_edit(context)
    context.user_data["_to_delete"] = []
    _track(context, update.message)

    msg = await update.message.reply_text(
        "✏️ *Редагування*\n\nВведіть *ID транзакції*:",
        parse_mode="Markdown",
        reply_markup=CANCEL_KEYBOARD,
    )
    _track(context, msg)
    return EDIT_WAIT_ID


async def edit_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if text == "❌ Скасувати":
        return await cancel(update, context)

    if not text.isdigit():
        msg = await update.message.reply_text(
            "❌ ID має бути числом. Спробуйте ще раз або *❌ Скасувати*.",
            parse_mode="Markdown",
        )
        _track(context, msg)
        return EDIT_WAIT_ID

    expense_id = int(text)
    wait_msg   = await update.message.reply_text("⏳ Шукаю…")
    _track(context, wait_msg)

    try:
        ws = await get_sheet()
        row_num, row = await asyncio.to_thread(_load_row, ws, expense_id)

        if not row_num:
            await wait_msg.edit_text(
                f"⚠️ Запис *#{expense_id}* не знайдено. Спробуйте інший ID або *❌ Скасувати*.",
                parse_mode="Markdown",
            )
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

        line = format_expense_line(
            context.user_data["edit_category"],
            context.user_data["edit_article"],
            context.user_data["edit_amount"],
            context.user_data["edit_currency"],
        )
        # Видаляємо сервісні; залишаємо лише повідомлення з вибором поля
        await _cleanup(context)

        msg = await update.message.reply_text(
            f"✏️ *Редагування #{expense_id}*\n\n{line}\n\nОберіть що змінити:",
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
        await _cleanup(context)
        await query.message.delete()
        _clear_edit(context)
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    # ── Зберегти ──────────────────────────────────────────────────────────────
    if data == "ef_save":
        await query.edit_message_text("⏳ Зберігаю…")
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
            line = format_expense_line(ed["edit_category"], ed["edit_article"], amount_val, currency)
            # Замінюємо повідомлення вибору полів на підсумок
            await query.edit_message_text(
                f"✅ *Запис #{exp_id} оновлено!*\n\n{line}",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("edit save error: %s", exc)
            await query.edit_message_text("❌ Помилка збереження.")

        await _cleanup(context)
        _clear_edit(context)
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
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
        hint = "⚠️ Для «Інше» стаття обов'язкова.\n" if cat == "Інше" else "Або /skip щоб стаття = категорія.\n"
        # Залишаємо повідомлення вибору полів, додаємо тимчасове
        await query.edit_message_text(
            f"📝 *Введіть нову статтю:*\n{hint}",
            parse_mode="Markdown",
        )
        msg = await query.message.reply_text("↓", reply_markup=CANCEL_KEYBOARD)
        _track(context, msg)
        return EDIT_ART

    if data == "ef_amt":
        await query.edit_message_text(
            "💰 *Введіть нову суму та валюту:*\n\n"
            "`500 UAH` · `50 EUR` · `100 USD` · `200 PLN` · `150 GBP`",
            parse_mode="Markdown",
        )
        msg = await query.message.reply_text("↓", reply_markup=CANCEL_KEYBOARD)
        _track(context, msg)
        return EDIT_AMT

    return EDIT_CHOOSE_FIELD


async def edit_new_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "efc_cancel":
        # Повертаємось до вибору полів
        return await _edit_show_fields(query, context)

    idx      = int(query.data.split("_")[1])
    category = CATEGORIES[idx]
    context.user_data["edit_category"] = category
    return await _edit_show_fields(query, context)


async def edit_new_article(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if text == "❌ Скасувати":
        await _cleanup(context)
        return await _edit_show_fields_msg(update.message, context)

    if text == "/skip":
        category = context.user_data.get("edit_category", "")
        if category == "Інше":
            msg = await update.message.reply_text(
                "⚠️ Для «Інше» стаття обов'язкова. Введіть опис:",
                reply_markup=CANCEL_KEYBOARD,
            )
            _track(context, msg)
            return EDIT_ART
        context.user_data["edit_article"] = category
    else:
        context.user_data["edit_article"] = text

    await _cleanup(context)
    return await _edit_show_fields_msg(update.message, context)


async def edit_new_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if text == "❌ Скасувати":
        await _cleanup(context)
        return await _edit_show_fields_msg(update.message, context)

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


async def _edit_show_fields(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Оновити поточне повідомлення з inline-кнопками вибору полів."""
    ed   = context.user_data
    line = format_expense_line(
        ed.get("edit_category", "—"), ed.get("edit_article",  "—"),
        ed.get("edit_amount",   "—"), ed.get("edit_currency", "EUR"),
    )
    await query.edit_message_text(
        f"✏️ *Запис #{ed.get('edit_id', '?')}*\n\n{line}\n\nЩо ще змінити або збережіть:",
        parse_mode="Markdown",
        reply_markup=build_edit_field_keyboard(),
    )
    return EDIT_CHOOSE_FIELD


async def _edit_show_fields_msg(message: Message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Надіслати нове повідомлення з вибором полів (після текстового введення)."""
    ed   = context.user_data
    line = format_expense_line(
        ed.get("edit_category", "—"), ed.get("edit_article",  "—"),
        ed.get("edit_amount",   "—"), ed.get("edit_currency", "EUR"),
    )
    msg = await message.reply_text(
        f"✏️ *Запис #{ed.get('edit_id', '?')}*\n\n{line}\n\nЩо ще змінити або збережіть:",
        parse_mode="Markdown",
        reply_markup=build_edit_field_keyboard(),
    )
    _track(context, msg)  # це повідомлення теж видалимо при завершенні
    return EDIT_CHOOSE_FIELD


def _clear_edit(context: ContextTypes.DEFAULT_TYPE):
    for key in ("edit_id", "edit_row_num", "edit_ws", "edit_date", "edit_name",
                "edit_category", "edit_article", "edit_amount", "edit_currency"):
        context.user_data.pop(key, None)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_to_delete"] = []
    _track(context, update.message)

    msg = await update.message.reply_text(
        "🗑️ *Видалення*\n\nВведіть *ID транзакції*:",
        parse_mode="Markdown",
        reply_markup=CANCEL_KEYBOARD,
    )
    _track(context, msg)
    return DEL_WAIT_ID


async def delete_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track(context, update.message)
    text = update.message.text.strip()

    if text == "❌ Скасувати":
        return await cancel(update, context)

    if not text.isdigit():
        msg = await update.message.reply_text(
            "❌ ID має бути числом. Спробуйте ще раз або *❌ Скасувати*.",
            parse_mode="Markdown",
        )
        _track(context, msg)
        return DEL_WAIT_ID

    expense_id = int(text)
    wait_msg   = await update.message.reply_text("⏳ Шукаю…")
    _track(context, wait_msg)

    try:
        ws = await get_sheet()
        row_num, row = await asyncio.to_thread(_load_row, ws, expense_id)

        if not row_num:
            await wait_msg.edit_text(
                f"⚠️ Запис *#{expense_id}* не знайдено. Спробуйте інший ID або *❌ Скасувати*.",
                parse_mode="Markdown",
            )
            return DEL_WAIT_ID

        context.user_data["del_id"] = expense_id
        context.user_data["del_ws"] = ws

        line = format_expense_line(
            row[2] if len(row) > 2 else "",
            row[3] if len(row) > 3 else "",
            row[4] if len(row) > 4 else "",
            row[5] if len(row) > 5 else "EUR",
        )
        # Видаляємо сервісні, показуємо підтвердження
        await _cleanup(context)

        msg = await update.message.reply_text(
            f"🗑️ *Видалення #{expense_id}*\n\n{line}\n\n"
            "⚠️ Впевнені що хочете *видалити* цей запис?",
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

    if query.data == "del_no":
        await _cleanup(context)
        await query.message.delete()
        context.user_data.pop("del_id", None)
        context.user_data.pop("del_ws", None)
        await query.message.reply_text("Натисніть кнопку коли знадобиться.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    # del_yes
    expense_id = context.user_data.get("del_id")
    ws         = context.user_data.get("del_ws")
    try:
        if not ws:
            ws = await get_sheet()
        row_num = await asyncio.to_thread(_find_row, ws, expense_id)
        if row_num:
            await sheet_delete_row(ws, row_num)
            # Замінюємо повідомлення підтвердження на підсумок
            await query.edit_message_text(
                f"🗑️ *Запис #{expense_id} видалено.*",
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

    await _cleanup(context)
    context.user_data.pop("del_id", None)
    context.user_data.pop("del_ws", None)
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

    add_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("add", add_expense),
            MessageHandler(filters.Regex(r"^➕ Додати витрату$") & filters.TEXT, add_expense),
        ],
        states={
            ADD_CATEGORY: [CallbackQueryHandler(add_category_chosen,  pattern=r"^(cat_\d+|cancel_conv)$")],
            ADD_ARTICLE:  [
                CommandHandler("skip", add_article_skipped),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_article_entered),
            ],
            ADD_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount_entered)],
            ADD_NEXT:     [CallbackQueryHandler(add_next_action, pattern=r"^(next_add|next_done)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(CANCEL_TEXT, cancel)],
    )

    edit_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^✏️ Змінити витрату$") & filters.TEXT, edit_start),
        ],
        states={
            EDIT_WAIT_ID:      [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_got_id)],
            EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_choose_field, pattern=r"^ef_(cat|art|amt|save|cancel)$")],
            EDIT_CAT:          [CallbackQueryHandler(edit_new_category, pattern=r"^(cat_\d+|efc_cancel)$")],
            EDIT_ART:          [
                CommandHandler("skip", edit_new_article),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_article),
            ],
            EDIT_AMT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(CANCEL_TEXT, cancel)],
    )

    del_conv = ConversationHandler(
        per_message=False,
        entry_points=[
            MessageHandler(filters.Regex(r"^🗑️ Видалити витрату$") & filters.TEXT, delete_start),
        ],
        states={
            DEL_WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_got_id)],
            DEL_CONFIRM: [CallbackQueryHandler(delete_confirm, pattern=r"^(del_yes|del_no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(CANCEL_TEXT, cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(del_conv)

    logger.info("Bot v4.2 running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
