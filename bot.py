import logging
import os
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes, CommandHandler
import gspread
from google.oauth2.service_account import Credentials
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────
TOKEN    = os.environ["BOT_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
ADMIN_ID = 879637514
USER2_ID = 753036716
ALLOWED  = [ADMIN_ID, USER2_ID]

# Категории
EXPENSE_CATS = ["🍔 Еда", "🚗 Транспорт", "🏠 ЖКХ", "💊 Здоровье",
                "👕 Одежда", "🎮 Развлечения", "☕ Кафе", "📱 Связь",
                "✈️ Путешествия", "🏋️ Спорт", "📚 Образование", "🐾 Другое"]
INCOME_CATS  = ["💼 Зарплата", "💵 Аванс", "🎁 Подарок", "💻 Фриланс",
                "📈 Инвестиции", "🔄 Перевод", "💰 Другое"]

MONTHS = ["Январь","Февраль","Март","Апрель","Май","Июнь",
          "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

# ── Google Sheets ────────────────────────────────────────────
def get_sheets():
    creds_json = os.environ["GOOGLE_CREDS"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SHEET_ID)
    return {
        "tx":    ss.worksheet("Transactions"),
        "goals": ss.worksheet("Goals"),
        "cats":  ss.worksheet("AllCategories"),
    }

def fmt(n):
    try:
        return f"{float(n):,.0f}".replace(",", " ")
    except:
        return str(n)

def pbar(pct):
    f = round(pct / 10)
    return "█" * f + "░" * (10 - f)

# ── Клавиатура ───────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    [["Расход", "Доход"], ["Статистика", "Цели"]],
    resize_keyboard=True,
    is_persistent=True
)

CANCEL_KB = ReplyKeyboardMarkup(
    [["❌ Отмена"]],
    resize_keyboard=True
)

# ── /start ───────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED:
        return
    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Используй кнопки внизу:\n\n"
        "Расход — записать трату\n"
        "Доход — записать поступление\n"
        "Статистика — посмотреть статистику\n"
        "Цели — цели накопления",
        reply_markup=MAIN_KB
    )

# ── Обработка текстовых сообщений ───────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED:
        return

    text = update.message.text.strip()
    state = ctx.user_data.get("state")

    # Отмена
    if text == "❌ Отмена":
        ctx.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return

    # Главные кнопки
    if text == "Расход":
        ctx.user_data.clear()
        ctx.user_data["state"]  = "awaiting_amount"
        ctx.user_data["tx_type"] = "Расход"
        await update.message.reply_text(
            "📉 Расход\n\nВведи сумму и описание:\n• 500 обед\n• 1200 такси\n• 45000",
            reply_markup=CANCEL_KB
        )
        return

    if text == "Доход":
        ctx.user_data.clear()
        ctx.user_data["state"]   = "awaiting_amount"
        ctx.user_data["tx_type"] = "Доход"
        await update.message.reply_text(
            "📈 Доход\n\nВведи сумму и описание:\n• 45000 зарплата\n• 5000 фриланс",
            reply_markup=CANCEL_KB
        )
        return

    if text == "Статистика":
        ctx.user_data.clear()
        await show_stats_menu(update, ctx)
        return

    if text == "Цели":
        ctx.user_data.clear()
        await show_goals_menu(update, ctx)
        return

    # Состояния
    if state == "awaiting_amount":
        await handle_amount(update, ctx, text)
        return

    if state == "awaiting_own_cat":
        await handle_own_category(update, ctx, text)
        return

    if state == "goal_name":
        ctx.user_data["goal_name"] = text.strip()
        ctx.user_data["state"]     = "goal_amount"
        await update.message.reply_text(
            f"✅ Название: {text.strip()}\n\nВведи сумму цели (₽):",
            reply_markup=CANCEL_KB
        )
        return

    if state == "goal_amount":
        try:
            amt = float(text.replace(" ", "").replace(",", "."))
            assert amt > 0
        except:
            await update.message.reply_text("⚠️ Введи корректную сумму:", reply_markup=CANCEL_KB)
            return
        ctx.user_data["goal_amount"] = amt
        ctx.user_data["state"]       = "goal_deadline"
        await update.message.reply_text(
            f"✅ Сумма: {fmt(amt)} ₽\n\nВведи дедлайн (например: 31.12.2025)\nили напиши нет:",
            reply_markup=ReplyKeyboardMarkup([["нет"], ["❌ Отмена"]], resize_keyboard=True)
        )
        return

    if state == "goal_deadline":
        deadline = "" if text.lower() == "нет" else text.strip()
        await finish_add_goal(update, ctx, deadline)
        return

    if state == "goal_deposit":
        await handle_goal_deposit(update, ctx, text)
        return

    # Ничего не совпало — показываем старт
    ctx.user_data.clear()
    await start(update, ctx)

# ── Ввод суммы ───────────────────────────────────────────────
async def handle_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    import re
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not match:
        await update.message.reply_text(
            "❓ Не нашёл сумму. Попробуй:\n• 500 обед\n• 1200",
            reply_markup=CANCEL_KB
        )
        return

    amount  = float(match.group(1).replace(" ", "").replace(",", "."))
    comment = text.replace(match.group(0), "").strip()
    tx_type = ctx.user_data["tx_type"]

    ctx.user_data["state"]      = "awaiting_cat"
    ctx.user_data["tx_amount"]  = amount
    ctx.user_data["tx_comment"] = comment

    await show_category_buttons(update, ctx, tx_type, amount, comment)

async def show_category_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tx_type, amount, comment):
    sheets   = get_sheets()
    all_rows = sheets["cats"].get_all_values()[1:]
    user_id  = str(update.effective_user.id)
    custom   = [r[1] for r in all_rows if len(r) > 1 and str(r[0]) == user_id and r[1]]

    default_cats = EXPENSE_CATS if tx_type == "Расход" else INCOME_CATS
    all_cats     = list(dict.fromkeys(default_cats + custom))

    # Кнопки по 2 в ряд
    rows = []
    for i in range(0, len(all_cats), 2):
        row = [InlineKeyboardButton(all_cats[i], callback_data=f"cat:{all_cats[i]}")]
        if i + 1 < len(all_cats):
            row.append(InlineKeyboardButton(all_cats[i+1], callback_data=f"cat:{all_cats[i+1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ Своя категория", callback_data="cat:__own__")])

    emoji = "📉" if tx_type == "Расход" else "📈"
    text  = f"{emoji} {tx_type}: {fmt(amount)} ₽"
    if comment:
        text += f"\n💬 {comment}"
    text += "\n\nВыбери категорию:"

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

# ── Обработка кнопок ─────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data
    await query.answer()

    if user_id not in ALLOWED:
        return

    # Категория транзакции
    if data.startswith("cat:"):
        cat = data[4:]
        if cat == "__own__":
            ctx.user_data["state"] = "awaiting_own_cat"
            await query.edit_message_text("✏️ Введи название своей категории:")
        else:
            await finish_tx(query, ctx, cat)
        return

    # Статистика
    if data == "stats_my":
        await send_my_stats(query, ctx)
        return
    if data == "stats_joint":
        await send_joint_stats(query, ctx)
        return

    # Цели
    if data == "goals_add":
        ctx.user_data.clear()
        ctx.user_data["state"] = "goal_name"
        await query.message.reply_text(
            "🎯 Новая цель\n\nВведи название\n(например: Отпуск, Машина, iPhone):",
            reply_markup=CANCEL_KB
        )
        return

    if data == "goals_list":
        await show_goals_list(query, ctx)
        return

    if data.startswith("goal_dep:"):
        goal_id = data[9:]
        ctx.user_data["state"]          = "goal_deposit"
        ctx.user_data["deposit_goal_id"] = goal_id
        sheets = get_sheets()
        rows   = sheets["goals"].get_all_values()[1:]
        goal   = next((r for r in rows if r[0] == goal_id and str(r[1]) == str(user_id)), None)
        if not goal:
            await query.edit_message_text("⚠️ Цель не найдена.")
            return
        await query.edit_message_text(
            f"➕ Пополнение «{goal[2]}»\n\n"
            f"Прогресс: {fmt(goal[4])} / {fmt(goal[3])} ₽\n\n"
            f"Введи сумму:"
        )
        return

    if data.startswith("goal_del:"):
        goal_id = data[9:]
        sheets  = get_sheets()
        rows    = sheets["goals"].get_all_values()
        for i, r in enumerate(rows[1:], start=2):
            if r[0] == goal_id and str(r[1]) == str(user_id):
                sheets["goals"].delete_rows(i)
                await query.edit_message_text("🗑 Цель удалена.")
                return
        await query.edit_message_text("⚠️ Цель не найдена.")
        return

# ── Завершение транзакции ────────────────────────────────────
async def finish_tx(query, ctx, category: str):
    user_id  = query.from_user.id
    tx_type  = ctx.user_data.get("tx_type",   "Расход")
    amount   = ctx.user_data.get("tx_amount",  0)
    comment  = ctx.user_data.get("tx_comment", "")

    sheets = get_sheets()
    sheets["tx"].append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_id, tx_type, amount, category, comment
    ])

    # Баланс
    rows = sheets["tx"].get_all_values()[1:]
    inc  = sum(float(r[3]) for r in rows if str(r[1]) == str(user_id) and r[2] == "Доход")
    exp  = sum(float(r[3]) for r in rows if str(r[1]) == str(user_id) and r[2] == "Расход")

    emoji = "📉" if tx_type == "Расход" else "📈"
    text  = (
        f"✅ Записано!\n\n"
        f"{emoji} {tx_type}: {fmt(amount)} ₽\n"
        f"🏷 {category}"
        + (f"\n💬 {comment}" if comment else "") +
        f"\n\n💰 Баланс: {fmt(inc - exp)} ₽"
    )

    ctx.user_data.clear()
    await query.edit_message_text(text)
    await query.message.reply_text("👇", reply_markup=MAIN_KB)

async def handle_own_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    cat     = text.strip()
    user_id = update.effective_user.id
    sheets  = get_sheets()

    # Сохраняем кастомную категорию
    all_rows = sheets["cats"].get_all_values()[1:]
    exists   = any(str(r[0]) == str(user_id) and r[1] == cat for r in all_rows if len(r) > 1)
    if not exists:
        sheets["cats"].append_row([user_id, cat])

    # Завершаем транзакцию
    tx_type  = ctx.user_data.get("tx_type",   "Расход")
    amount   = ctx.user_data.get("tx_amount",  0)
    comment  = ctx.user_data.get("tx_comment", "")

    sheets["tx"].append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_id, tx_type, amount, cat, comment
    ])

    rows = sheets["tx"].get_all_values()[1:]
    inc  = sum(float(r[3]) for r in rows if str(r[1]) == str(user_id) and r[2] == "Доход")
    exp  = sum(float(r[3]) for r in rows if str(r[1]) == str(user_id) and r[2] == "Расход")

    emoji = "📉" if tx_type == "Расход" else "📈"
    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ Записано!\n\n"
        f"{emoji} {tx_type}: {fmt(amount)} ₽\n"
        f"🏷 {cat}"
        + (f"\n💬 {comment}" if comment else "") +
        f"\n\n💰 Баланс: {fmt(inc - exp)} ₽",
        reply_markup=MAIN_KB
    )

# ── Статистика ───────────────────────────────────────────────
async def show_stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Моя статистика",   callback_data="stats_my")],
        [InlineKeyboardButton("👥 Общая статистика", callback_data="stats_joint")]
    ])
    await update.message.reply_text("📊 Выбери статистику:", reply_markup=kb)

async def send_my_stats(query, ctx):
    user_id = query.from_user.id
    sheets  = get_sheets()
    rows    = sheets["tx"].get_all_values()[1:]
    now     = datetime.now()
    m, y    = now.month - 1, now.year

    tot_inc = tot_exp = m_inc = m_exp = 0.0
    cats = {}

    for r in rows:
        if not r[0] or str(r[1]) != str(user_id):
            continue
        try:
            amt  = float(r[3])
            date = datetime.strptime(r[0][:10], "%Y-%m-%d")
            cat  = r[4] or "Другое"
        except:
            continue

        if r[2] == "Доход": tot_inc += amt
        else:               tot_exp += amt

        if date.month - 1 == m and date.year == y:
            if r[2] == "Доход": m_inc += amt
            else:
                m_exp      += amt
                cats[cat]   = cats.get(cat, 0) + amt

    top = "\n".join(f"  {c} — {fmt(v)} ₽" for c, v in sorted(cats.items(), key=lambda x: -x[1])[:5]) or "  нет данных"

    text = (
        f"👤 Твоя статистика\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 {MONTHS[m]} {y}:\n"
        f"📈 Доходы:  {fmt(m_inc)} ₽\n"
        f"📉 Расходы: {fmt(m_exp)} ₽\n"
        f"💰 Баланс:  {fmt(m_inc - m_exp)} ₽\n\n"
        f"🏆 Топ расходов:\n{top}\n\n"
        f"📊 За всё время:\n"
        f"📈 {fmt(tot_inc)} ₽  |  📉 {fmt(tot_exp)} ₽\n"
        f"💰 Итого: {fmt(tot_inc - tot_exp)} ₽"
    )
    await query.edit_message_text(text)

async def send_joint_stats(query, ctx):
    sheets = get_sheets()
    rows   = sheets["tx"].get_all_values()[1:]
    now    = datetime.now()
    m, y   = now.month - 1, now.year

    tot_inc = tot_exp = m_inc = m_exp = 0.0
    per_user = {}

    for r in rows:
        if not r[0]:
            continue
        try:
            amt  = float(r[3])
            uid  = str(r[1])
            date = datetime.strptime(r[0][:10], "%Y-%m-%d")
        except:
            continue

        if uid not in per_user:
            per_user[uid] = {"inc": 0.0, "exp": 0.0}

        if r[2] == "Доход": tot_inc += amt; per_user[uid]["inc"] += amt
        else:               tot_exp += amt; per_user[uid]["exp"] += amt

        if date.month - 1 == m and date.year == y:
            if r[2] == "Доход": m_inc += amt
            else:               m_exp += amt

    lines = ""
    for uid, v in per_user.items():
        lbl    = "Пользователь 1" if uid == str(ADMIN_ID) else "Пользователь 2"
        lines += f"\n{lbl}:\n  📈 {fmt(v['inc'])} ₽  |  📉 {fmt(v['exp'])} ₽\n  💰 {fmt(v['inc']-v['exp'])} ₽"

    text = (
        f"👥 Общая статистика\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 {MONTHS[m]} {y}:\n"
        f"📈 Доходы:  {fmt(m_inc)} ₽\n"
        f"📉 Расходы: {fmt(m_exp)} ₽\n"
        f"💰 Баланс:  {fmt(m_inc - m_exp)} ₽\n\n"
        f"📊 За всё время:\n"
        f"📈 {fmt(tot_inc)} ₽  |  📉 {fmt(tot_exp)} ₽\n"
        f"💰 Итого: {fmt(tot_inc - tot_exp)} ₽\n\n"
        f"👥 По пользователям:{lines}"
    )
    await query.edit_message_text(text)

# ── Цели ─────────────────────────────────────────────────────
async def show_goals_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sheets  = get_sheets()
    rows    = [r for r in sheets["goals"].get_all_values()[1:] if r[0] and str(r[1]) == str(user_id)]
    count   = len(rows)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить цель", callback_data="goals_add")],
        [InlineKeyboardButton("📋 Мои цели",      callback_data="goals_list")]
    ])
    msg = "🎯 Целей пока нет.\nДобавь первую!" if count == 0 else f"🎯 Цели накопления ({count})"
    await update.message.reply_text(msg, reply_markup=kb)

async def show_goals_list(query, ctx):
    user_id = query.from_user.id
    sheets  = get_sheets()
    rows    = [r for r in sheets["goals"].get_all_values()[1:] if r[0] and str(r[1]) == str(user_id)]

    if not rows:
        await query.edit_message_text("🎯 Нет целей. Нажми «Добавить цель».")
        return

    text = "🎯 Твои цели:\n━━━━━━━━━━━━━━━\n"
    btns = []

    for r in rows:
        try:
            target  = float(r[3])
            current = float(r[4]) if r[4] else 0.0
        except:
            target = current = 0.0

        pct     = min(100, round(current / target * 100)) if target > 0 else 0
        remains = max(0, target - current)
        dl      = f"\n📅 До: {r[5]}" if len(r) > 5 and r[5] else ""

        text += (
            f"\n🎯 {r[2]}\n"
            f"{pbar(pct)} {pct}%\n"
            f"💰 {fmt(current)} / {fmt(target)} ₽\n"
            f"📌 Осталось: {fmt(remains)} ₽{dl}\n"
        )
        btns.append([
            InlineKeyboardButton(f"➕ {r[2]}", callback_data=f"goal_dep:{r[0]}"),
            InlineKeyboardButton("🗑",          callback_data=f"goal_del:{r[0]}")
        ])

    btns.append([InlineKeyboardButton("➕ Добавить цель", callback_data="goals_add")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns))

async def finish_add_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deadline: str):
    name    = ctx.user_data.get("goal_name", "")
    amount  = ctx.user_data.get("goal_amount", 0)
    user_id = update.effective_user.id
    goal_id = f"g_{user_id}_{int(datetime.now().timestamp())}"

    sheets = get_sheets()
    sheets["goals"].append_row([goal_id, user_id, name, amount, 0, deadline])

    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ Цель создана!\n\n"
        f"🎯 {name}\n"
        f"💰 {fmt(amount)} ₽\n"
        f"{'📅 До: ' + deadline if deadline else '📅 Без срока'}",
        reply_markup=MAIN_KB
    )

async def handle_goal_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        amt = float(text.replace(" ", "").replace(",", "."))
        assert amt > 0
    except:
        await update.message.reply_text("⚠️ Введи корректную сумму:", reply_markup=CANCEL_KB)
        return

    goal_id = ctx.user_data.get("deposit_goal_id")
    user_id = update.effective_user.id
    sheets  = get_sheets()
    rows    = sheets["goals"].get_all_values()

    for i, r in enumerate(rows[1:], start=2):
        if r[0] == goal_id and str(r[1]) == str(user_id):
            current = float(r[4]) if r[4] else 0.0
            new_val = current + amt
            sheets["goals"].update_cell(i, 5, new_val)
            target  = float(r[3])
            pct     = min(100, round(new_val / target * 100)) if target > 0 else 0
            done    = new_val >= target
            ctx.user_data.clear()
            await update.message.reply_text(
                f"✅ Пополнено на {fmt(amt)} ₽\n\n"
                f"🎯 {r[2]}\n"
                f"{pbar(pct)} {pct}%\n"
                f"{fmt(new_val)} / {fmt(target)} ₽\n\n"
                f"{'🎉 Цель достигнута! Поздравляю!' if done else f'📌 Осталось: {fmt(max(0, target - new_val))} ₽'}",
                reply_markup=MAIN_KB
            )
            return

    await update.message.reply_text("⚠️ Цель не найдена.", reply_markup=MAIN_KB)

# ── Запуск ───────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
