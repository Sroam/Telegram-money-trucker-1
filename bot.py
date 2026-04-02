"""
Expense Tracker Bot — Single File Version
All logic in one file for simple deployment.
"""
import os, io, re, json, csv, logging, sqlite3, sys
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.getenv("OPENAI_API_KEY", "")
DB_PATH          = os.getenv("DATABASE_PATH", "expenses.db")
ALLOWED_IDS      = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS","").split(",") if x.strip().isdigit()]
PARSE_MODEL      = os.getenv("PARSE_MODEL", "claude-haiku-4-5-20251001")
VOICE_PROVIDER   = os.getenv("VOICE_PROVIDER", "openai")

if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_BOT_TOKEN is required")
if not ANTHROPIC_KEY:  raise ValueError("ANTHROPIC_API_KEY is required")

# ── Models ────────────────────────────────────────────────────────────────────
CATEGORY_EMOJI = {
    "food":"🍔","coffee":"☕","supermarket":"🛒","gas":"⛽",
    "cigarettes":"🚬","shopping":"🛍️","entertainment":"🎬",
    "transport":"🚌","bills":"📄","health":"💊","home":"🏠",
    "clothes":"👕","other":"💰"
}
CATEGORY_LABELS = {
    "food":"Food","coffee":"Coffee","supermarket":"Supermarket","gas":"Gas / Fuel",
    "cigarettes":"Cigarettes","shopping":"Shopping","entertainment":"Entertainment",
    "transport":"Transport","bills":"Bills","health":"Health",
    "home":"Home","clothes":"Clothes","other":"Other"
}

@dataclass
class Expense:
    id: Optional[int]
    user_id: int
    amount: float
    currency: str
    category: str
    merchant: Optional[str]
    description: str
    raw_input: str
    date: date
    created_at: datetime

    @property
    def emoji(self): return CATEGORY_EMOJI.get(self.category, "💰")
    @property
    def merchant_str(self): return f" @ {self.merchant}" if self.merchant else ""

# ── Database ──────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def db_init():
    with db_connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'EUR', category TEXT NOT NULL DEFAULT 'other',
                merchant TEXT, description TEXT DEFAULT '', raw_input TEXT DEFAULT '',
                date DATE NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_exp_user ON expenses(user_id, date);
        """)
    logger.info(f"DB ready: {DB_PATH}")

def db_upsert_user(uid, username, first_name):
    with db_connect() as c:
        c.execute("INSERT INTO users(telegram_id,username,first_name) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",
                  (uid, username, first_name))

def db_add(e: Expense) -> int:
    with db_connect() as c:
        cur = c.execute("INSERT INTO expenses(user_id,amount,currency,category,merchant,description,raw_input,date) VALUES(?,?,?,?,?,?,?,?)",
            (e.user_id,e.amount,e.currency,e.category,e.merchant,e.description,e.raw_input,e.date.isoformat()))
        return cur.lastrowid

def db_delete_last(uid) -> Optional[Expense]:
    with db_connect() as c:
        row = c.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(uid,)).fetchone()
        if not row: return None
        exp = row_to_exp(row)
        c.execute("DELETE FROM expenses WHERE id=?",(row["id"],))
        return exp

def db_get_date(uid, d: date) -> List[Expense]:
    with db_connect() as c:
        rows = c.execute("SELECT * FROM expenses WHERE user_id=? AND date=? ORDER BY created_at DESC",(uid,d.isoformat())).fetchall()
    return [row_to_exp(r) for r in rows]

def db_get_range(uid, start: date, end: date) -> List[Expense]:
    with db_connect() as c:
        rows = c.execute("SELECT * FROM expenses WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY date DESC,created_at DESC",(uid,start.isoformat(),end.isoformat())).fetchall()
    return [row_to_exp(r) for r in rows]

def db_get_last(uid, n=10) -> List[Expense]:
    with db_connect() as c:
        rows = c.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(uid,n)).fetchall()
    return [row_to_exp(r) for r in rows]

def db_get_all(uid) -> List[Expense]:
    with db_connect() as c:
        rows = c.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY date DESC,created_at DESC",(uid,)).fetchall()
    return [row_to_exp(r) for r in rows]

def db_category_totals(uid, start: date, end: date) -> Dict[str,float]:
    with db_connect() as c:
        rows = c.execute("SELECT category,SUM(amount) as t FROM expenses WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY category ORDER BY t DESC",(uid,start.isoformat(),end.isoformat())).fetchall()
    return {r["category"]:round(r["t"],2) for r in rows}

def db_total(uid, start: date, end: date) -> float:
    with db_connect() as c:
        row = c.execute("SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?",(uid,start.isoformat(),end.isoformat())).fetchone()
    return round(row["t"],2)

def row_to_exp(row) -> Expense:
    return Expense(id=row["id"],user_id=row["user_id"],amount=row["amount"],currency=row["currency"],
        category=row["category"],merchant=row["merchant"],description=row["description"],
        raw_input=row["raw_input"],date=date.fromisoformat(row["date"]),
        created_at=datetime.fromisoformat(row["created_at"]))

# ── Parser (Claude AI) ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expense parsing assistant. Extract ALL expenses from natural, messy, multilingual text.

INPUT can be English, Greek, German, Greeklish, Dutch, or any mix. Voice-to-text errors are common.
The input may contain ONE or MULTIPLE expenses mentioned together.

OUTPUT: Return ONLY a valid JSON array. No explanation, no markdown, no extra text.
Always return an ARRAY even for a single expense: [{"amount":...}, ...]

Each item schema:
{"amount": <number>, "currency": "EUR", "category": <string>, "merchant": <string or null>, "description": <string>, "date_offset": <0=today, -1=yesterday>, "confidence": <float 0-1>}

CATEGORIES (use exactly): food, coffee, supermarket, gas, cigarettes, shopping, entertainment, transport, bills, health, home, clothes, other

RULES:
- coffee/kaffee/kafe/καφές/espresso/latte/cappuccino/frappe → "coffee"
- food/essen/φαγητό/φαΐ/restaurant/pizza/souvlaki/gyros/γύρος/burger/delivery/wolt/voedsel → "food"
- supermarket/einkaufen/ψώνια/psonia/boodschappen → "supermarket"
- lidl/aldi/rewe/penny/edeka/netto/spar/billa/σκλαβενίτης/ab → "supermarket"
- gas/tanken/βενζίνη/venzini/petrol/fuel/benzine/benzina → "gas"
- shell/bp/aral/esso/total/revoil → "gas"
- cigarettes/zigaretten/τσιγάρα/tsigara/marlboro/winston/sigaretten → "cigarettes"
- transport/bus/taxi/metro/uber/ubahn → "transport"
- bills/rechnung/ΔΕΗ/ΟΤΕ/internet/electricity → "bills"
- health/apotheke/pharmacy/φαρμακείο → "health"
- home/ikea/obi/leroy → "home"
- clothes/zara/h&m/primark/ρούχα → "clothes"
- entertainment/kino/cinema/netflix/spotify/bar → "entertainment"

AMOUNTS: 10/10.50/10,50/€10/10€/10 euro/zehn/δέκα/tien/twintig/twin → number
Word numbers: tien=10, twintig=20, dertig=30, veertig=40, vijftig=50 (Dutch)
              zehn=10, zwanzig=20, dreißig=30 (German)
              δέκα=10, είκοσι=20, τριάντα=30, σαράντα=40, πενήντα=50 (Greek)
DATES: today/heute/σήμερα=0, yesterday/gestern/χτες=-1

MULTIPLE EXPENSE EXAMPLES:
"20€ βενζίνη, 15€ τσιγάρα και 10€ γύρος" → [
  {"amount":20,"currency":"EUR","category":"gas","merchant":null,"description":"fuel","date_offset":0,"confidence":0.95},
  {"amount":15,"currency":"EUR","category":"cigarettes","merchant":null,"description":"cigarettes","date_offset":0,"confidence":0.95},
  {"amount":10,"currency":"EUR","category":"food","merchant":null,"description":"gyros","date_offset":0,"confidence":0.95}
]
"twintig euro benzine, tien euro sigaretten en twintig euro voedsel" → [
  {"amount":20,"currency":"EUR","category":"gas","merchant":null,"description":"fuel","date_offset":0,"confidence":0.9},
  {"amount":10,"currency":"EUR","category":"cigarettes","merchant":null,"description":"cigarettes","date_offset":0,"confidence":0.9},
  {"amount":20,"currency":"EUR","category":"food","merchant":null,"description":"food","date_offset":0,"confidence":0.9}
]
"lidl 30" → [{"amount":30,"currency":"EUR","category":"supermarket","merchant":"Lidl","description":"Lidl supermarket","date_offset":0,"confidence":0.95}]
"βενζίνη 50" → [{"amount":50,"currency":"EUR","category":"gas","merchant":null,"description":"fuel","date_offset":0,"confidence":0.95}]

If no valid expense found, return empty array: []"""

MERCHANT_MAP = {"lidl":"Lidl","aldi":"Aldi","rewe":"Rewe","penny":"Penny","edeka":"Edeka",
    "netto":"Netto","spar":"Spar","billa":"Billa","shell":"Shell","bp":"BP","aral":"Aral",
    "esso":"Esso","total":"Total","revoil":"Revoil","starbucks":"Starbucks","costa":"Costa",
    "zara":"Zara","ikea":"IKEA","obi":"OBI","h&m":"H&M","primark":"Primark"}

def parse_expenses(text: str, today: date = None) -> List[dict]:
    """Parse text and return a LIST of expenses (supports multiple in one message)."""
    if today is None: today = date.today()
    text = text.strip()
    if not text: return []
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(model=PARSE_MODEL, max_tokens=800, system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":text}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        try: data = json.loads(raw)
        except:
            m = re.search(r"\[.*\]",raw,re.DOTALL)
            data = json.loads(m.group()) if m else []
        if not isinstance(data, list): data = [data] if isinstance(data, dict) else []
        results = []
        for item in data:
            if not item or item.get("amount") is None: continue
            amount = float(item["amount"])
            if amount <= 0: continue
            offset = int(item.get("date_offset", 0))
            merchant = item.get("merchant")
            if merchant:
                merchant = MERCHANT_MAP.get(merchant.lower(), merchant.title())
            results.append({
                "amount": round(amount, 2),
                "currency": item.get("currency", "EUR"),
                "category": item.get("category", "other"),
                "merchant": merchant,
                "description": item.get("description", text)[:100],
                "date": today + timedelta(days=offset),
                "confidence": float(item.get("confidence", 0.5))
            })
        return results
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return []

# Keep single parse for backward compat
def parse_expense(text: str, today: date = None) -> Optional[dict]:
    results = parse_expenses(text, today)
    return results[0] if results else None

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(audio_bytes: bytes) -> Optional[str]:
    if not OPENAI_KEY: return None
    try:
        import openai
        client = openai.OpenAI(api_key=OPENAI_KEY)
        f = io.BytesIO(audio_bytes); f.name = "audio.ogg"
        resp = client.audio.transcriptions.create(model="whisper-1", file=f,
            prompt="Expense note. Amounts in euros. May be Greek, German, or English.")
        return resp.text.strip()
    except Exception as e:
        logger.error(f"Whisper error: {e}"); return None

# ── Reports ───────────────────────────────────────────────────────────────────
def week_range():
    t = date.today(); return t - timedelta(days=t.weekday()), t

def month_range():
    t = date.today(); return t.replace(day=1), t

def progress_bar(pct, w=8):
    f = round(pct/100*w)
    return "█"*f + "░"*(w-f)

def fmt_list(expenses, title, show_total=False):
    if not expenses:
        return f"*{title}*\n\nΔεν υπάρχουν έξοδα. Στείλε μου ένα μήνυμα!"
    lines = [f"*{title}*\n"]
    cur_date = None
    for e in expenses:
        if show_total and e.date != cur_date:
            cur_date = e.date
            lines.append(f"\n📅 _{e.date.strftime('%A, %d %b')}_")
        lines.append(f"{e.emoji} €{e.amount:.2f}  {e.description}{e.merchant_str}" +
                     (f"  _(#{e.id})_" if e.id else ""))
    if show_total:
        total = sum(e.amount for e in expenses)
        lines.append(f"\n💶 *Σύνολο: €{total:.2f}*")
    return "\n".join(lines)

def fmt_categories(uid, start, end, title):
    totals = db_category_totals(uid, start, end)
    grand = db_total(uid, start, end)
    if not totals: return f"📊 *{title}*\n\nΔεν υπάρχουν έξοδα."
    lines = [f"📊 *{title}*\n"]
    for cat, amt in totals.items():
        pct = (amt/grand*100) if grand > 0 else 0
        label = CATEGORY_LABELS.get(cat, cat.title())
        lines.append(f"{CATEGORY_EMOJI.get(cat,'💰')} *{label}*\n   €{amt:.2f}  {progress_bar(pct)}  {pct:.0f}%")
    lines.append(f"\n💶 *Σύνολο: €{grand:.2f}*")
    return "\n".join(lines)

def fmt_report(uid, start, end, title):
    totals = db_category_totals(uid, start, end)
    grand = db_total(uid, start, end)
    expenses = db_get_range(uid, start, end)
    if not expenses: return f"📋 *{title}*\n\nΔεν υπάρχουν έξοδα."
    days = max((end-start).days+1,1)
    lines = [f"📋 *{title}*",
             f"Περίοδος: {start.strftime('%d %b')} – {end.strftime('%d %b %Y')}",
             f"",f"💶 *Σύνολο: €{grand:.2f}*",
             f"📊 Μ.Ο. ημέρας: €{grand/days:.2f}",
             f"📝 Συναλλαγές: {len(expenses)}","","*Ανά κατηγορία:*"]
    for cat, amt in totals.items():
        lines.append(f"  {CATEGORY_EMOJI.get(cat,'💰')} {CATEGORY_LABELS.get(cat,cat)}: €{amt:.2f}")
    top5 = sorted(expenses, key=lambda e: e.amount, reverse=True)[:5]
    lines.append("\n*Μεγαλύτερα έξοδα:*")
    for i,e in enumerate(top5,1):
        lines.append(f"  {i}. {e.emoji} €{e.amount:.2f} — {e.description[:25]}{e.merchant_str} ({e.date.strftime('%d %b')})")
    return "\n".join(lines)

# ── Export ────────────────────────────────────────────────────────────────────
def to_csv(expenses):
    out = io.StringIO()
    w = csv.writer(out, quoting=csv.QUOTE_ALL)
    w.writerow(["ID","Date","Amount","Currency","Category","Merchant","Description","Raw Input"])
    for e in expenses:
        w.writerow([e.id,e.date.isoformat(),f"{e.amount:.2f}",e.currency,
            CATEGORY_LABELS.get(e.category,e.category),e.merchant or "",e.description,e.raw_input])
    return out.getvalue().encode("utf-8-sig")

# ── Messages ──────────────────────────────────────────────────────────────────
HELP = """🤖 *Expense Tracker Bot*

*Πρόσθεσε έξοδο* — στείλε μήνυμα ή 🎤 φωνητικό:
  • `10 euro coffee`
  • `βενζίνη 50`
  • `lidl 30`
  • `gestern 15 essen`

*Προβολή εξόδων:*
  /today — Σήμερα
  /week — Αυτή η εβδομάδα
  /month — Αυτός ο μήνας
  /expenses — Τελευταίες 10
  /last — Τελευταίες 5

*Αναφορές:*
  /report — Μηνιαία αναφορά
  /categories — Ανά κατηγορία
  /top — Μεγαλύτερα έξοδα

*Διαγραφή:*
  /undo — Διέγραψε το τελευταίο

*Εξαγωγή:*
  /export — CSV αρχείο"""

START = """👋 *Καλώς ήρθες στο Expense Tracker!*

Στείλε μου ένα μήνυμα ή 🎤 φωνητικό:
  • `10 euro coffee`
  • `βενζίνη 50`
  • `lidl 30`
  • `gestern tanken 45`

/help για όλες τις εντολές."""

# ── Telegram Bot ──────────────────────────────────────────────────────────────
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

def allowed(uid): return not ALLOWED_IDS or uid in ALLOWED_IDS

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not allowed(u.id): return
    db_upsert_user(u.id, u.username or "", u.first_name or "")
    await update.message.reply_text(START, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id): return
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    exps = db_get_date(uid, date.today())
    await update.message.reply_text(fmt_list(exps, "📅 Σήμερα", show_total=True), parse_mode=ParseMode.MARKDOWN)

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    s, e = week_range()
    exps = db_get_range(uid, s, e)
    await update.message.reply_text(fmt_list(exps, f"📆 Εβδομάδα ({s.strftime('%d %b')} – {e.strftime('%d %b')})", show_total=True), parse_mode=ParseMode.MARKDOWN)

async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    s, e = month_range()
    exps = db_get_range(uid, s, e)
    await update.message.reply_text(fmt_list(exps, f"🗓 {s.strftime('%B %Y')}", show_total=True), parse_mode=ParseMode.MARKDOWN)

async def cmd_expenses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    exps = db_get_last(uid, 10)
    lines = ["*📋 Τελευταίες 10*\n"]
    for e in exps:
        lines.append(f"{e.emoji} *€{e.amount:.2f}* — {e.description}{e.merchant_str}  _{e.date.strftime('%d %b')}_  _(#{e.id})_")
    if exps:
        lines.append(f"\n💶 *Σύνολο: €{sum(e.amount for e in exps):.2f}*")
    await update.message.reply_text("\n".join(lines) if exps else "Δεν υπάρχουν έξοδα.", parse_mode=ParseMode.MARKDOWN)

async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    exps = db_get_last(uid, 5)
    lines = ["*🕐 Τελευταίες 5*\n"]
    for e in exps: lines.append(f"{e.emoji} *€{e.amount:.2f}* — {e.description}{e.merchant_str}  _{e.date.strftime('%d %b')}_")
    await update.message.reply_text("\n".join(lines) if exps else "Δεν υπάρχουν έξοδα.", parse_mode=ParseMode.MARKDOWN)

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    s, e = month_range()
    await update.message.reply_text(fmt_report(uid, s, e, f"Μηνιαία Αναφορά — {s.strftime('%B %Y')}"), parse_mode=ParseMode.MARKDOWN)

async def cmd_categories(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    s, e = month_range()
    await update.message.reply_text(fmt_categories(uid, s, e, f"Κατηγορίες — {s.strftime('%B %Y')}"), parse_mode=ParseMode.MARKDOWN)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    exps = sorted(db_get_last(uid, 50), key=lambda e: e.amount, reverse=True)[:10]
    await update.message.reply_text(fmt_list(exps, "🏆 Top 10 Έξοδα"), parse_mode=ParseMode.MARKDOWN)

async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    deleted = db_delete_last(uid)
    if deleted:
        await update.message.reply_text(f"🗑 *Διαγράφηκε:*\n€{deleted.amount:.2f} — {deleted.description}{deleted.merchant_str}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Δεν βρέθηκαν έξοδα για διαγραφή.")

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    exps = db_get_all(uid)
    if not exps: await update.message.reply_text("Δεν υπάρχουν έξοδα για εξαγωγή."); return
    await update.message.reply_document(document=io.BytesIO(to_csv(exps)),
        filename=f"expenses_{date.today().isoformat()}.csv",
        caption=f"📊 {len(exps)} έξοδα — {date.today().strftime('%d %b %Y')}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not allowed(u.id): return
    db_upsert_user(u.id, u.username or "", u.first_name or "")
    await process(update, u.id, update.message.text.strip())

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not allowed(u.id): return
    db_upsert_user(u.id, u.username or "", u.first_name or "")
    msg = await update.message.reply_text("🎤 Μεταγραφή φωνητικού...")
    try:
        file = await ctx.bot.get_file(update.message.voice.file_id)
        audio = await file.download_as_bytearray()
    except Exception as e:
        await msg.edit_text("❌ Σφάλμα κατεβάσματος φωνητικού."); return
    text = transcribe_voice(bytes(audio))
    if not text: await msg.edit_text("❌ Δεν μπόρεσα να μεταγράψω. Δοκίμασε να πληκτρολογήσεις."); return
    await msg.edit_text(f"🎤 _Άκουσα: {text}_", parse_mode=ParseMode.MARKDOWN)
    await process(update, u.id, text)

async def process(update, uid, text):
    results = parse_expenses(text)
    if not results:
        await update.message.reply_text(
            f"🤔 Δεν κατάλαβα: _{text}_\n\nΔοκίμασε:\n• `10 euro coffee`\n• `βενζίνη 50`\n• `lidl 30`",
            parse_mode=ParseMode.MARKDOWN); return

    # Save all expenses
    saved = []
    for result in results:
        exp = Expense(id=None, user_id=uid, amount=result["amount"], currency=result["currency"],
            category=result["category"], merchant=result["merchant"], description=result["description"],
            raw_input=text, date=result["date"], created_at=datetime.now())
        exp.id = db_add(exp)
        saved.append(exp)

    # Single expense — detailed reply
    if len(saved) == 1:
        exp = saved[0]
        date_label = "Σήμερα" if exp.date == date.today() else exp.date.strftime("%d %b %Y")
        cat_label = CATEGORY_LABELS.get(exp.category, exp.category.title())
        lines = [f"✅ *Καταγράφηκε!*\n",
                 f"💶 *Ποσό:* €{exp.amount:.2f}",
                 f"{exp.emoji} *Κατηγορία:* {cat_label}"]
        if exp.merchant: lines.append(f"🏪 *Κατάστημα:* {exp.merchant}")
        lines.append(f"📅 *Ημερομηνία:* {date_label}")
        lines.append(f"📝 *Σημείωση:* {exp.description}")
        lines.append(f"\n_Στείλε άλλο έξοδο ή χρησιμοποίησε /today_")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # Multiple expenses — summary reply
    else:
        total = sum(e.amount for e in saved)
        lines = [f"✅ *{len(saved)} έξοδα καταγράφηκαν!*\n"]
        for exp in saved:
            cat_label = CATEGORY_LABELS.get(exp.category, exp.category.title())
            merchant_str = f" @ {exp.merchant}" if exp.merchant else ""
            lines.append(f"{exp.emoji} €{exp.amount:.2f} — {cat_label}{merchant_str}")
        lines.append(f"\n💶 *Σύνολο: €{total:.2f}*")
        lines.append(f"\n_Χρησιμοποίησε /today για να δεις όλα τα σημερινά_")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    db_init()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("expenses", cmd_expenses))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("delete_last", cmd_undo))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
