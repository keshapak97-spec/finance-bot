import logging
import os
import json
import re
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram import WebAppInfo
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes, CommandHandler
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────
TOKEN    = os.environ["BOT_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
ADMIN_ID = 879637514
USER2_ID = 753036716
ALLOWED  = [ADMIN_ID, USER2_ID]

EXPENSE_CATS = ["🍔 Еда", "🚗 Транспорт", "🏠 ЖКХ", "💊 Здоровье",
                "👕 Одежда", "🎮 Развлечения", "☕ Кафе", "📱 Связь",
                "✈️ Путешествия", "🏋️ Спорт", "📚 Образование", "🐾 Другое"]
INCOME_CATS  = ["💼 Зарплата", "💵 Аванс", "🎁 Подарок", "💻 Фриланс",
                "📈 Инвестиции", "🔄 Перевод", "💰 Другое"]

MONTHS = ["Январь","Февраль","Март","Апрель","Май","Июнь",
          "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

# ── Google Sheets ────────────────────────────────────────────
def get_sheets():
    creds_dict = json.loads(os.environ["GOOGLE_CREDS"])
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
    try: return f"{float(n):,.0f}".replace(",", " ")
    except: return str(n)

def pbar(pct):
    f = round(pct / 10)
    return "█" * f + "░" * (10 - f)

# ── Клавиатуры ───────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["Расход", "Доход"],
        ["Статистика", "Цели"],
        [KeyboardButton("📱 Mini App", web_app=WebAppInfo(url="https://" + os.environ.get('GITHUB_PAGES_URL','')))]
    ],
    resize_keyboard=True,
    is_persistent=True
)
CANCEL_KB = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)

# ╔══════════════════════════════════════════════════════════════╗
# ║  API СЕРВЕР ДЛЯ MINI APP                                   ║
# ╚══════════════════════════════════════════════════════════════╝

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Отключаем лишние логи

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/api/stats":
            user_id = params.get("userId", [None])[0]
            if not user_id:
                self.send_json({"error": "no userId"}, 400); return
            self.send_json(get_stats(user_id))

        elif path == "/api/goals":
            user_id = params.get("userId", [None])[0]
            if not user_id:
                self.send_json({"error": "no userId"}, 400); return
            self.send_json(get_goals_data(user_id))

        elif path == "/api/transactions":
            user_id = params.get("userId", [None])[0]
            limit   = int(params.get("limit", [30])[0])
            if not user_id:
                self.send_json({"error": "no userId"}, 400); return
            self.send_json(get_transactions(user_id, limit))

        elif path == "/health":
            self.send_json({"status": "ok"})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed  = urlparse(self.path)
        path    = parsed.path
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/goals/add":
            result = add_goal(body.get("userId"), body.get("name"), body.get("target"), body.get("deadline",""))
            self.send_json(result)
        elif path == "/api/goals/deposit":
            result = deposit_goal(body.get("userId"), body.get("goalId"), body.get("amount"))
            self.send_json(result)
        elif path == "/api/goals/delete":
            result = delete_goal_api(body.get("userId"), body.get("goalId"))
            self.send_json(result)
        else:
            self.send_json({"error": "not found"}, 404)

def get_stats(user_id):
    sheets = get_sheets()
    rows   = sheets["tx"].get_all_values()[1:]
    now    = datetime.now()
    m, y   = now.month - 1, now.year

    p = {"income":0,"expense":0,"monthIncome":0,"monthExpense":0,"cats":{},"monthly":{}}
    j = {"income":0,"expense":0,"monthIncome":0,"monthExpense":0,"monthly":{}}

    for r in rows:
        if not r[0]: continue
        try:
            amt  = float(r[3])
            date = datetime.strptime(r[0][:10], "%Y-%m-%d")
            cat  = r[4] or "Другое"
            uid  = str(r[1])
            typ  = r[2]
            mk   = f"{date.year}-{date.month:02d}"
        except: continue

        is_me = uid == str(user_id)

        if typ == "Доход":
            j["income"] += amt
            j["monthly"].setdefault(mk, {"income":0,"expense":0})
            j["monthly"][mk]["income"] += amt
            if is_me:
                p["income"] += amt
                p["monthly"].setdefault(mk, {"income":0,"expense":0})
                p["monthly"][mk]["income"] += amt
        else:
            j["expense"] += amt
            j["monthly"].setdefault(mk, {"income":0,"expense":0})
            j["monthly"][mk]["expense"] += amt
            if is_me:
                p["expense"] += amt
                p["cats"][cat] = p["cats"].get(cat, 0) + amt
                p["monthly"].setdefault(mk, {"income":0,"expense":0})
                p["monthly"][mk]["expense"] += amt

        if date.month - 1 == m and date.year == y:
            if typ == "Доход":
                j["monthIncome"] += amt
                if is_me: p["monthIncome"] += amt
            else:
                j["monthExpense"] += amt
                if is_me: p["monthExpense"] += amt

    return {"personal": p, "joint": j, "month": MONTHS[m], "year": y}

def get_goals_data(user_id):
    sheets = get_sheets()
    rows   = sheets["goals"].get_all_values()[1:]
    goals  = []
    for r in rows:
        if r[0] and str(r[1]) == str(user_id):
            goals.append({
                "id": r[0], "name": r[2],
                "target": float(r[3]) if r[3] else 0,
                "current": float(r[4]) if r[4] else 0,
                "deadline": r[5] if len(r) > 5 else ""
            })
    return goals

def get_transactions(user_id, limit=30):
    sheets = get_sheets()
    rows   = sheets["tx"].get_all_values()[1:]
    result = []
    for r in reversed(rows):
        if not r[0] or str(r[1]) != str(user_id): continue
        result.append({
            "date": r[0][:10], "type": r[2],
            "amount": float(r[3]) if r[3] else 0,
            "category": r[4], "comment": r[5] if len(r) > 5 else ""
        })
        if len(result) >= limit: break
    return result

def add_goal(user_id, name, target, deadline):
    if not all([user_id, name, target]): return {"success": False}
    sheets  = get_sheets()
    goal_id = f"g_{user_id}_{int(datetime.now().timestamp())}"
    sheets["goals"].append_row([goal_id, user_id, name, float(target), 0, deadline or ""])
    return {"success": True, "id": goal_id}

def deposit_goal(user_id, goal_id, amount):
    if not all([user_id, goal_id, amount]): return {"success": False}
    sheets = get_sheets()
    rows   = sheets["goals"].get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if r[0] == goal_id and str(r[1]) == str(user_id):
            new_val = (float(r[4]) if r[4] else 0) + float(amount)
            sheets["goals"].update_cell(i, 5, new_val)
            return {"success": True, "current": new_val}
    return {"success": False}

def delete_goal_api(user_id, goal_id):
    if not all([user_id, goal_id]): return {"success": False}
    sheets = get_sheets()
    rows   = sheets["goals"].get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if r[0] == goal_id and str(r[1]) == str(user_id):
            sheets["goals"].delete_rows(i)
            return {"success": True}
    return {"success": False}

def start_api_server():
    port   = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), APIHandler)
    logger.info(f"API server on port {port}")
    server.serve_forever()

# ╔══════════════════════════════════════════════════════════════╗
# ║  БОТ                                                        ║
# ╚══════════════════════════════════════════════════════════════╝

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED: return
    ctx.user_data.clear()
    user_id = update.effective_user.id
    app_url = f"https://{os.environ.get('GITHUB_PAGES_URL', '')}?userId={user_id}"

    kb = ReplyKeyboardMarkup(
        [
            ["Расход", "Доход"],
            ["Статистика", "Цели"],
            [KeyboardButton("📱 Mini App", web_app=WebAppInfo(url=app_url))]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

    await update.message.reply_text(
        "👋 Привет! Используй кнопки внизу:\n\n"
        "Расход — записать трату\n"
        "Доход — записать поступление\n"
        "Статистика — посмотреть статистику\n"
        "Цели — цели накопления\n"
        "📱 Mini App — подробная статистика",
        reply_markup=kb
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED: return
    text  = update.message.text.strip()
    state = ctx.user_data.get("state")

    if text == "❌ Отмена":
        ctx.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return

    if text == "Расход":
        ctx.user_data.clear()
        ctx.user_data["state"]   = "awaiting_amount"
        ctx.user_data["tx_type"] = "Расход"
        await update.message.reply_text(
            "📉 Расход\n\nВведи сумму и описание:\n• 500 обед\n• 1200 такси\n• 45000",
            reply_markup=CANCEL_KB)
        return

    if text == "Доход":
        ctx.user_data.clear()
        ctx.user_data["state"]   = "awaiting_amount"
        ctx.user_data["tx_type"] = "Доход"
        await update.message.reply_text(
            "📈 Доход\n\nВведи сумму и описание:\n• 45000 зарплата\n• 5000 фриланс",
            reply_markup=CANCEL_KB)
        return

    if text == "Статистика":
        ctx.user_data.clear()
        await show_stats_menu(update, ctx)
        return

    if text == "Цели":
        ctx.user_data.clear()
        await show_goals_menu(update, ctx)
        return

    if state == "awaiting_amount":
        await handle_amount(update, ctx, text); return
    if state == "awaiting_own_cat":
        await handle_own_category(update, ctx, text); return
    if state == "goal_name":
        ctx.user_data["goal_name"] = text.strip()
        ctx.user_data["state"]     = "goal_amount"
        await update.message.reply_text(f"✅ Название: {text.strip()}\n\nВведи сумму цели (₽):", reply_markup=CANCEL_KB)
        return
    if state == "goal_amount":
        try:
            amt = float(text.replace(" ","").replace(",",".")); assert amt > 0
        except:
            await update.message.reply_text("⚠️ Введи корректную сумму:", reply_markup=CANCEL_KB); return
        ctx.user_data["goal_amount"] = amt
        ctx.user_data["state"]       = "goal_deadline"
        await update.message.reply_text(
            f"✅ Сумма: {fmt(amt)} ₽\n\nВведи дедлайн (например: 31.12.2025)\nили напиши нет:",
            reply_markup=ReplyKeyboardMarkup([["нет"],["❌ Отмена"]], resize_keyboard=True))
        return
    if state == "goal_deadline":
        deadline = "" if text.lower() == "нет" else text.strip()
        await finish_add_goal(update, ctx, deadline); return
    if state == "goal_deposit":
        await handle_goal_deposit(update, ctx, text); return

    ctx.user_data.clear()
    await start(update, ctx)

async def handle_amount(update, ctx, text):
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not match:
        await update.message.reply_text("❓ Не нашёл сумму. Попробуй:\n• 500 обед\n• 1200", reply_markup=CANCEL_KB)
        return
    amount  = float(match.group(1).replace(" ","").replace(",","."))
    comment = text.replace(match.group(0), "").strip()
    ctx.user_data["state"]      = "awaiting_cat"
    ctx.user_data["tx_amount"]  = amount
    ctx.user_data["tx_comment"] = comment
    await show_category_buttons(update, ctx, ctx.user_data["tx_type"], amount, comment)

async def show_category_buttons(update, ctx, tx_type, amount, comment):
    sheets   = get_sheets()
    all_rows = sheets["cats"].get_all_values()[1:]
    user_id  = str(update.effective_user.id)
    custom   = [r[1] for r in all_rows if len(r)>1 and str(r[0])==user_id and r[1]]
    default  = EXPENSE_CATS if tx_type == "Расход" else INCOME_CATS
    all_cats = list(dict.fromkeys(default + custom))

    rows = []
    for i in range(0, len(all_cats), 2):
        row = [InlineKeyboardButton(all_cats[i], callback_data=f"cat:{all_cats[i]}")]
        if i+1 < len(all_cats):
            row.append(InlineKeyboardButton(all_cats[i+1], callback_data=f"cat:{all_cats[i+1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ Своя категория", callback_data="cat:__own__")])

    emoji = "📉" if tx_type == "Расход" else "📈"
    text  = f"{emoji} {tx_type}: {fmt(amount)} ₽"
    if comment: text += f"\n💬 {comment}"
    text += "\n\nВыбери категорию:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data
    await query.answer()
    if user_id not in ALLOWED: return

    if data.startswith("cat:"):
        cat = data[4:]
        if cat == "__own__":
            ctx.user_data["state"] = "awaiting_own_cat"
            await query.edit_message_text("✏️ Введи название своей категории:")
        else:
            await finish_tx(query, ctx, cat)
        return

    if data == "stats_my":    await send_my_stats(query, ctx);    return
    if data == "stats_joint": await send_joint_stats(query, ctx); return
    if data == "goals_add":
        ctx.user_data.clear()
        ctx.user_data["state"] = "goal_name"
        await query.message.reply_text("🎯 Новая цель\n\nВведи название:", reply_markup=CANCEL_KB)
        return
    if data == "goals_list":
        await show_goals_list(query, ctx); return
    if data.startswith("goal_dep:"):
        goal_id = data[9:]
        ctx.user_data["state"]           = "goal_deposit"
        ctx.user_data["deposit_goal_id"] = goal_id
        sheets = get_sheets()
        rows   = sheets["goals"].get_all_values()[1:]
        goal   = next((r for r in rows if r[0]==goal_id and str(r[1])==str(user_id)), None)
        if not goal: await query.edit_message_text("⚠️ Цель не найдена."); return
        await query.edit_message_text(
            f"➕ Пополнение «{goal[2]}»\n\nПрогресс: {fmt(goal[4])} / {fmt(goal[3])} ₽\n\nВведи сумму:")
        return
    if data.startswith("goal_del:"):
        goal_id = data[9:]
        sheets  = get_sheets()
        rows    = sheets["goals"].get_all_values()
        for i, r in enumerate(rows[1:], start=2):
            if r[0]==goal_id and str(r[1])==str(user_id):
                sheets["goals"].delete_rows(i)
                await query.edit_message_text("🗑 Цель удалена.")
                return
        await query.edit_message_text("⚠️ Цель не найдена.")

async def finish_tx(query, ctx, category):
    user_id = query.from_user.id
    tx_type = ctx.user_data.get("tx_type","Расход")
    amount  = ctx.user_data.get("tx_amount", 0)
    comment = ctx.user_data.get("tx_comment","")
    sheets  = get_sheets()
    sheets["tx"].append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, tx_type, amount, category, comment])
    rows = sheets["tx"].get_all_values()[1:]
    inc  = sum(float(r[3]) for r in rows if str(r[1])==str(user_id) and r[2]=="Доход")
    exp  = sum(float(r[3]) for r in rows if str(r[1])==str(user_id) and r[2]=="Расход")
    emoji = "📉" if tx_type == "Расход" else "📈"
    ctx.user_data.clear()
    await query.edit_message_text(
        f"✅ Записано!\n\n{emoji} {tx_type}: {fmt(amount)} ₽\n🏷 {category}"
        + (f"\n💬 {comment}" if comment else "")
        + f"\n\n💰 Баланс: {fmt(inc-exp)} ₽")
    await query.message.reply_text("👇", reply_markup=MAIN_KB)

async def handle_own_category(update, ctx, text):
    cat     = text.strip()
    user_id = update.effective_user.id
    sheets  = get_sheets()
    rows    = sheets["cats"].get_all_values()[1:]
    if not any(str(r[0])==str(user_id) and r[1]==cat for r in rows if len(r)>1):
        sheets["cats"].append_row([user_id, cat])
    tx_type = ctx.user_data.get("tx_type","Расход")
    amount  = ctx.user_data.get("tx_amount", 0)
    comment = ctx.user_data.get("tx_comment","")
    sheets["tx"].append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, tx_type, amount, cat, comment])
    rows = sheets["tx"].get_all_values()[1:]
    inc  = sum(float(r[3]) for r in rows if str(r[1])==str(user_id) and r[2]=="Доход")
    exp  = sum(float(r[3]) for r in rows if str(r[1])==str(user_id) and r[2]=="Расход")
    emoji = "📉" if tx_type == "Расход" else "📈"
    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ Записано!\n\n{emoji} {tx_type}: {fmt(amount)} ₽\n🏷 {cat}"
        + (f"\n💬 {comment}" if comment else "")
        + f"\n\n💰 Баланс: {fmt(inc-exp)} ₽",
        reply_markup=MAIN_KB)

async def show_stats_menu(update, ctx):
    await update.message.reply_text("📊 Выбери статистику:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Моя статистика",   callback_data="stats_my")],
        [InlineKeyboardButton("👥 Общая статистика", callback_data="stats_joint")]
    ]))

async def send_my_stats(query, ctx):
    data    = get_stats(query.from_user.id)
    p       = data["personal"]
    top     = "\n".join(f"  {c} — {fmt(v)} ₽" for c,v in sorted(p["cats"].items(),key=lambda x:-x[1])[:5]) or "  нет данных"
    await query.edit_message_text(
        f"👤 Твоя статистика\n━━━━━━━━━━━━━━━\n"
        f"📅 {data['month']} {data['year']}:\n"
        f"📈 Доходы:  {fmt(p['monthIncome'])} ₽\n"
        f"📉 Расходы: {fmt(p['monthExpense'])} ₽\n"
        f"💰 Баланс:  {fmt(p['monthIncome']-p['monthExpense'])} ₽\n\n"
        f"🏆 Топ расходов:\n{top}\n\n"
        f"📊 За всё время:\n"
        f"📈 {fmt(p['income'])} ₽  |  📉 {fmt(p['expense'])} ₽\n"
        f"💰 Итого: {fmt(p['income']-p['expense'])} ₽")

async def send_joint_stats(query, ctx):
    data = get_stats(ADMIN_ID)
    j    = data["joint"]
    sheets  = get_sheets()
    rows    = sheets["tx"].get_all_values()[1:]
    per_user = {}
    for r in rows:
        if not r[0]: continue
        try: amt = float(r[3]); uid = str(r[1])
        except: continue
        if uid not in per_user: per_user[uid] = {"inc":0,"exp":0}
        if r[2]=="Доход": per_user[uid]["inc"]+=amt
        else:             per_user[uid]["exp"]+=amt
    lines = ""
    for uid,v in per_user.items():
        lbl = "Пользователь 1" if uid==str(ADMIN_ID) else "Пользователь 2"
        lines += f"\n{lbl}:\n  📈 {fmt(v['inc'])} ₽  |  📉 {fmt(v['exp'])} ₽\n  💰 {fmt(v['inc']-v['exp'])} ₽"
    await query.edit_message_text(
        f"👥 Общая статистика\n━━━━━━━━━━━━━━━\n"
        f"📅 {data['month']} {data['year']}:\n"
        f"📈 Доходы:  {fmt(j['monthIncome'])} ₽\n"
        f"📉 Расходы: {fmt(j['monthExpense'])} ₽\n"
        f"💰 Баланс:  {fmt(j['monthIncome']-j['monthExpense'])} ₽\n\n"
        f"📊 За всё время:\n"
        f"📈 {fmt(j['income'])} ₽  |  📉 {fmt(j['expense'])} ₽\n"
        f"💰 Итого: {fmt(j['income']-j['expense'])} ₽\n\n"
        f"👥 По пользователям:{lines}")

async def show_goals_menu(update, ctx):
    user_id = update.effective_user.id
    goals   = get_goals_data(user_id)
    msg     = "🎯 Целей пока нет.\nДобавь первую!" if not goals else f"🎯 Цели накопления ({len(goals)})"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить цель", callback_data="goals_add")],
        [InlineKeyboardButton("📋 Мои цели",      callback_data="goals_list")]
    ]))

async def show_goals_list(query, ctx):
    user_id = query.from_user.id
    goals   = get_goals_data(user_id)
    if not goals:
        await query.edit_message_text("🎯 Нет целей."); return
    text = "🎯 Твои цели:\n━━━━━━━━━━━━━━━\n"
    btns = []
    for g in goals:
        pct     = min(100, round(g["current"]/g["target"]*100)) if g["target"]>0 else 0
        remains = max(0, g["target"]-g["current"])
        dl      = f"\n📅 До: {g['deadline']}" if g["deadline"] else ""
        text   += f"\n🎯 {g['name']}\n{pbar(pct)} {pct}%\n💰 {fmt(g['current'])} / {fmt(g['target'])} ₽\n📌 Осталось: {fmt(remains)} ₽{dl}\n"
        btns.append([
            InlineKeyboardButton(f"➕ {g['name']}", callback_data=f"goal_dep:{g['id']}"),
            InlineKeyboardButton("🗑",               callback_data=f"goal_del:{g['id']}")
        ])
    btns.append([InlineKeyboardButton("➕ Добавить цель", callback_data="goals_add")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns))

async def finish_add_goal(update, ctx, deadline):
    name    = ctx.user_data.get("goal_name","")
    amount  = ctx.user_data.get("goal_amount",0)
    user_id = update.effective_user.id
    add_goal(user_id, name, amount, deadline)
    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ Цель создана!\n\n🎯 {name}\n💰 {fmt(amount)} ₽\n"
        f"{'📅 До: '+deadline if deadline else '📅 Без срока'}",
        reply_markup=MAIN_KB)

async def handle_goal_deposit(update, ctx, text):
    try: amt = float(text.replace(" ","").replace(",",".")); assert amt>0
    except:
        await update.message.reply_text("⚠️ Введи корректную сумму:", reply_markup=CANCEL_KB); return
    goal_id = ctx.user_data.get("deposit_goal_id")
    user_id = update.effective_user.id
    result  = deposit_goal(user_id, goal_id, amt)
    if not result["success"]:
        await update.message.reply_text("⚠️ Цель не найдена.", reply_markup=MAIN_KB); return
    goals = get_goals_data(user_id)
    goal  = next((g for g in goals if g["id"]==goal_id), None)
    if goal:
        pct  = min(100, round(goal["current"]/goal["target"]*100)) if goal["target"]>0 else 0
        done = goal["current"] >= goal["target"]
        ctx.user_data.clear()
        await update.message.reply_text(
            f"✅ Пополнено на {fmt(amt)} ₽\n\n🎯 {goal['name']}\n{pbar(pct)} {pct}%\n"
            f"{fmt(goal['current'])} / {fmt(goal['target'])} ₽\n\n"
            f"{'🎉 Цель достигнута!' if done else f'📌 Осталось: {fmt(max(0,goal[chr(116)+'arget']-goal[chr(99)+'urrent']))} ₽'}",
            reply_markup=MAIN_KB)

import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

async def send_reminder(app):
    for uid in ALLOWED:
        try:
            await app.bot.send_message(
                chat_id=uid,
                text="💰 Не забудь записать расходы и доходы за сегодня!",
                reply_markup=MAIN_KB
            )
        except Exception as e:
            logger.error(f"Reminder error for {uid}: {e}")

def main():
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Планировщик напоминаний
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_reminder, "cron", hour=15, minute=0,  args=[app])
    scheduler.add_job(send_reminder, "cron", hour=22, minute=30, args=[app])
    scheduler.start()

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
