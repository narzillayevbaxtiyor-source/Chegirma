import os
import re
import json
import time
import sqlite3
import asyncio
from typing import Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")

DB_PATH = (os.getenv("DB_PATH") or "/tmp/watchlist.db").strip()
CHECK_EVERY_SEC = int((os.getenv("CHECK_EVERY_SEC") or "900").strip() or "900")
USER_AGENT = (os.getenv("USER_AGENT") or "Mozilla/5.0").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables ga BOT_TOKEN qo‘ying.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID yo‘q. Railway Variables ga ADMIN_ID qo‘ying.")

# ---------------- DB ----------------
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            trigger_price REAL NOT NULL,
            category TEXT,
            last_price REAL,
            last_checked_ts INTEGER,
            created_ts INTEGER,
            is_active INTEGER DEFAULT 1,
            last_error TEXT
        )
    """)
    conn.commit()
    conn.close()

# -------------- helpers --------------
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if "..." in url:
        raise ValueError("Link to‘liq emas (Telegram qisqartirgan). Brauzerdan to‘liq linkni nusxa qilib yuboring.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Link http:// yoki https:// bilan boshlanishi kerak.")
    url = url.replace(" ", "%20")
    return url

def parse_float(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("\u00a0", " ")
    s = s.replace("٬", ",").replace("،", ",")
    m = re.findall(r"[0-9][0-9\.,]*", s)
    if not m:
        return None
    raw = m[0]
    if "," in raw and "." in raw:
        if raw.rfind(".") > raw.rfind(","):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    else:
        if raw.count(",") == 1 and raw.count(".") == 0:
            raw = raw.replace(",", ".")
        elif raw.count(",") > 1 and "." not in raw:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except:
        return None

async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        text = await resp.text(errors="ignore")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
        return text

def extract_price_from_html(html: str) -> Optional[float]:
    soup = BeautifulSoup(html, "lxml")

    for attr, key in [
        ("property", "product:price:amount"),
        ("property", "og:price:amount"),
        ("name", "price"),
        ("itemprop", "price"),
    ]:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            p = parse_float(tag["content"])
            if p is not None:
                return p

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text(strip=True) or "{}")
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if isinstance(obj, dict):
                    offers = obj.get("offers")
                    if isinstance(offers, dict):
                        p = parse_float(str(offers.get("price", "")))
                        if p is not None:
                            return p
                    if isinstance(offers, list):
                        for off in offers:
                            if isinstance(off, dict):
                                p = parse_float(str(off.get("price", "")))
                                if p is not None:
                                    return p
        except:
            pass

    text = soup.get_text(" ", strip=True)
    for pat in [
        r"(SAR|ر\.س|SR)\s*([0-9][0-9\.,]+)",
        r"([0-9][0-9\.,]+)\s*(SAR|ر\.س|SR)",
        r"(\$|USD)\s*([0-9][0-9\.,]+)",
    ]:
        mm = re.search(pat, text, flags=re.IGNORECASE)
        if mm:
            for g in mm.groups():
                p = parse_float(str(g))
                if p is not None:
                    return p
    return None

async def check_one(session: aiohttp.ClientSession, url: str, trigger: float) -> Tuple[Optional[float], str, bool]:
    try:
        html = await fetch_html(session, url)
        price = extract_price_from_html(html)
        if price is None:
            return None, "❌ Narx topilmadi (sayt himoyalangan yoki struktura boshqacha).", False
        triggered = price <= trigger
        return price, f"✅ Narx: {price:g} (trigger: {trigger:g})", triggered
    except Exception as e:
        return None, f"❌ Sahifani o‘qishda xato: {e}", False

# -------------- commands --------------
def usage_text() -> str:
    return (
        "✅ *Chegirma kuzatuvchi bot (Railway + SQLite /tmp)*\n\n"
        "*Buyruqlar:*\n"
        "• `/add <url> <trigger_price> [category]`\n"
        "• `/list`\n"
        "• `/del <id>`\n"
        "• `/checkall`\n\n"
        "*Misol:*\n"
        "`/add https://sa.iherb.com/pr/xxxxx 120 VITAMIN`\n"
        "⚠️ Railway’da volume yo‘q bo‘lsa, restartdan keyin ro‘yxat o‘chadi.\n"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(usage_text(), parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔️ Ruxsat yo‘q.")
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Format: /add <url> <trigger_price> [category]")

    try:
        url = normalize_url(context.args[0])
    except Exception as e:
        return await update.message.reply_text(f"❌ Link xato: {e}")

    trigger = parse_float(context.args[1])
    if trigger is None:
        return await update.message.reply_text("❌ trigger_price raqam bo‘lsin. Misol: 120 yoki 120.5")

    category = " ".join(context.args[2:]).strip() if len(context.args) > 2 else None

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO watchlist(url, trigger_price, category, created_ts, last_checked_ts) VALUES(?,?,?,?,?)",
        (url, float(trigger), category, int(time.time()), 0),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    await update.message.reply_text(f"✅ Qo‘shildi. ID: {new_id}\n{url}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔️ Ruxsat yo‘q.")
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM watchlist WHERE is_active=1 ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("📭 Ro‘yxat bo‘sh.")

    parts = []
    for r in rows:
        lp = r["last_price"]
        lp_txt = f"{lp:g}" if lp is not None else "-"
        cat = r["category"] or "-"
        parts.append(f"#{r['id']}  trig={r['trigger_price']:g}  last={lp_txt}  cat={cat}\n{r['url']}")
    msg = "\n\n".join(parts)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n\n…(qisqartirildi)"
    await update.message.reply_text(msg)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔️ Ruxsat yo‘q.")
    if not context.args:
        return await update.message.reply_text("Format: /del <id>")
    try:
        item_id = int(context.args[0])
    except:
        return await update.message.reply_text("❌ ID raqam bo‘lishi kerak.")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE watchlist SET is_active=0 WHERE id=?", (item_id,))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    await update.message.reply_text("✅ O‘chirildi." if changed else "❌ Topilmadi.")

async def cmd_checkall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔️ Ruxsat yo‘q.")
    await update.message.reply_text("🔎 Tekshirish boshlandi...")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM watchlist WHERE is_active=1 ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("📭 Ro‘yxat bo‘sh.")

    alerts = []
    async with aiohttp.ClientSession() as session:
        for r in rows:
            price, info, triggered = await check_one(session, r["url"], float(r["trigger_price"]))

            conn2 = db_conn()
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE watchlist SET last_price=?, last_checked_ts=?, last_error=? WHERE id=?",
                (price, int(time.time()), (None if info.startswith("✅") else info), r["id"]),
            )
            conn2.commit()
            conn2.close()

            if triggered:
                alerts.append(f"🔥 *TRIGGER*  #{r['id']}\n{info}\n{r['url']}")

    if alerts:
        msg = "\n\n".join(alerts)
        if len(msg) > 3800:
            msg = msg[:3800] + "\n\n…(qisqartirildi)"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await update.message.reply_text("✅ Hozircha trigger yo‘q.")

# -------- background loop (JobQueue yo'q) --------
async def background_loop(app: Application):
    await asyncio.sleep(5)
    while True:
        try:
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM watchlist WHERE is_active=1 ORDER BY id ASC")
            rows = cur.fetchall()
            conn.close()

            if rows:
                async with aiohttp.ClientSession() as session:
                    for r in rows:
                        price, info, triggered = await check_one(session, r["url"], float(r["trigger_price"]))

                        conn2 = db_conn()
                        cur2 = conn2.cursor()
                        cur2.execute(
                            "UPDATE watchlist SET last_price=?, last_checked_ts=?, last_error=? WHERE id=?",
                            (price, int(time.time()), (None if info.startswith("✅") else info), r["id"]),
                        )
                        conn2.commit()
                        conn2.close()

                        if triggered:
                            await app.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=f"🔥 *TRIGGER*  #{r['id']}\n{info}\n{r['url']}",
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True,
                            )
        except Exception as e:
            print("BG ERROR:", e)

        await asyncio.sleep(CHECK_EVERY_SEC)

async def post_init(app: Application):
    app.create_task(background_loop(app))

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("checkall", cmd_checkall))

    print("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
