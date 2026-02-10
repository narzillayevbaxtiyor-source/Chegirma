import os
import re
import json
import time
import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# ENV
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")

# SQLite (Railway volume bo'lmasa /tmp ishlaydi, restartda ketadi)
DB_PATH = (os.getenv("DB_PATH") or "/tmp/watchlist.db").strip()

CHECK_EVERY_SEC = int((os.getenv("CHECK_EVERY_SEC") or "900").strip() or "900")  # 15 min default
SAR_PER_USD = float((os.getenv("SAR_PER_USD") or "3.70").strip() or "3.70")

AUTO_POST_TO_CHANNEL = int((os.getenv("AUTO_POST_TO_CHANNEL") or "0").strip() or "0")
CHANNEL_ID = int((os.getenv("CHANNEL_ID") or "0").strip() or "0")

UA = (os.getenv("USER_AGENT") or
      "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Mobile Safari/537.36").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables ga BOT_TOKEN qo‘ying.")

# =========================
# DB
# =========================
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        clean_url TEXT NOT NULL,
        title TEXT,
        category TEXT,
        trigger_price REAL NOT NULL,
        sell_price REAL,
        last_price REAL,
        currency TEXT,
        last_checked_ts INTEGER,
        alerted INTEGER DEFAULT 0,
        created_ts INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

# =========================
# URL CLEAN + REDIRECT RESOLVE
# =========================
def extract_first_url(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"(https?://[^\s]+)", text)
    if not m:
        return text.strip()
    return m.group(1).strip()

def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    raw = extract_first_url(raw)

    # tozalash
    raw = raw.strip("()[]<>{}.,;\"'")

    # double slashes: https://www.extra.com//en-sa -> https://www.extra.com/en-sa
    raw = re.sub(r"(?<!:)//+", "/", raw)
    raw = raw.replace("https:/", "https://").replace("http:/", "http://")

    p = urlparse(raw)

    if not p.scheme:
        raise ValueError("URL scheme yo‘q (https://...)")
    if not p.netloc:
        raise ValueError("URL host (domain) yo‘q yoki noto‘g‘ri")

    # tracking querylarni olib tashlash
    q2 = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_"):
            continue
        if kl in ("fbclid", "gclid", "igshid", "mc_cid", "mc_eid"):
            continue
        if kl in ("shareid", "scm", "spm", "ref", "ref_"):
            continue
        q2.append((k, v))

    clean = p._replace(query=urlencode(q2, doseq=True), fragment="")
    return urlunparse(clean)

def resolve_redirects(url: str, timeout=20) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    s = requests.Session()
    s.headers.update(headers)

    # HEAD -> GET fallback
    try:
        r = s.head(url, allow_redirects=True, timeout=timeout)
        if r.url:
            return r.url
    except Exception:
        pass

    r = s.get(url, allow_redirects=True, timeout=timeout)
    return r.url or url

def prepare_url(raw: str) -> str:
    u = normalize_url(raw)
    u = resolve_redirects(u)
    u = normalize_url(u)
    return u

# =========================
# FETCH + PARSE
# =========================
def fetch_html(url: str, timeout=25) -> str:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

def _meta(soup: BeautifulSoup, name=None, prop=None) -> Optional[str]:
    if prop:
        t = soup.find("meta", attrs={"property": prop})
        return t.get("content") if t and t.get("content") else None
    if name:
        t = soup.find("meta", attrs={"name": name})
        return t.get("content") if t and t.get("content") else None
    return None

def extract_title_price(html: str) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    soup = BeautifulSoup(html, "lxml")

    title = _meta(soup, prop="og:title") or _meta(soup, name="twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    # currency
    currency = _meta(soup, prop="product:price:currency") or _meta(soup, prop="og:price:currency")

    # 1) meta price
    price_s = _meta(soup, prop="product:price:amount") or _meta(soup, prop="og:price:amount")

    # 2) JSON-LD
    if not price_s:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            txt = tag.string
            if not txt:
                continue
            try:
                data = json.loads(txt)
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for it in items:
                if not isinstance(it, dict):
                    continue
                offers = it.get("offers")

                if isinstance(offers, dict):
                    p = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                    c = offers.get("priceCurrency")
                    if p is not None and price_s is None:
                        price_s = str(p)
                        if c:
                            currency = currency or str(c)
                        break

                if isinstance(offers, list):
                    for off in offers:
                        if isinstance(off, dict):
                            p = off.get("price") or off.get("lowPrice")
                            c = off.get("priceCurrency")
                            if p is not None:
                                price_s = str(p)
                                if c:
                                    currency = currency or str(c)
                                break
                    if price_s:
                        break
            if price_s:
                break

    # 3) fallback: qidirish (SAR, ر.س, USD)
    price_val = None
    if price_s:
        try:
            # "1,299.00" -> 1299.00
            price_val = float(re.sub(r"[^\d.]", "", price_s.replace(",", "")))
        except Exception:
            price_val = None

    if price_val is None:
        text = soup.get_text(" ", strip=True)
        # SAR / ر.س / USD raqamlarni topish
        m = re.search(r"(SAR|ر\.س|USD)\s*([0-9][0-9,]*\.?[0-9]*)", text, re.IGNORECASE)
        if m:
            currency = currency or m.group(1).upper().replace("ر.س", "SAR")
            try:
                price_val = float(m.group(2).replace(",", ""))
            except Exception:
                price_val = None

    # default currency guess
    if not currency:
        currency = "SAR"

    return title, price_val, currency

def to_sar(amount: float, currency: str) -> float:
    c = (currency or "").upper()
    if c == "USD":
        return amount * SAR_PER_USD
    return amount

# =========================
# BOT COMMANDS
# =========================
START_TEXT = """✅ *Deal Watcher (SQLite)* tayyor.

Buyruqlar:
• `/add <url> <trigger_price> [sell_price] [category]`
• `/list`
• `/item <id>`
• `/del <id>`
• `/checkall`

Misol:
`/add https://amzn.eu/d/xxxxx 120 160 SHOES`

Eslatma: Railway’da volume bo‘lmasa, SQLite *restartda o‘chishi mumkin*.
"""

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text or ""
    parts = msg.split()
    if len(parts) < 3:
        await update.message.reply_text("❌ Format: /add <url> <trigger_price> [sell_price] [category]")
        return

    raw_url = parts[1]
    try:
        trigger = float(parts[2])
    except Exception:
        await update.message.reply_text("❌ trigger_price raqam bo‘lsin. Masalan: 120")
        return

    sell_price = None
    category = None

    if len(parts) >= 4:
        # 4-chi argument raqam bo'lsa sell, bo'lmasa category
        try:
            sell_price = float(parts[3])
        except Exception:
            category = parts[3]

    if len(parts) >= 5:
        category = parts[4]

    # URLni tozalash + shortlink yechish
    try:
        clean_url = prepare_url(raw_url)
    except Exception as e:
        await update.message.reply_text(f"❌ Link xato: {e}")
        return

    # sahifani o'qib title/price olishga urinib ko'ramiz
    title = None
    price = None
    currency = None
    try:
        html = await asyncio.to_thread(fetch_html, clean_url)
        title, price, currency = extract_title_price(html)
    except Exception:
        pass

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO items (url, clean_url, title, category, trigger_price, sell_price, last_price, currency, last_checked_ts, alerted, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
    """, (
        raw_url,
        clean_url,
        title,
        (category or "").upper() if category else None,
        trigger,
        sell_price,
        price,
        currency,
        int(time.time()) if price is not None else None,
        int(time.time()),
    ))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()

    txt = f"✅ Qo‘shildi: #{item_id}\n{clean_url}\n🎯 trigger={trigger}"
    if sell_price is not None:
        txt += f"\n🏷 sell={sell_price}"
    if category:
        txt += f"\n📦 cat={category.upper()}"
    if title:
        txt += f"\n📝 {title}"
    if price is not None:
        txt += f"\n💰 hozir: {price} {currency or ''}"
    else:
        txt += "\n⚠️ Narx hozircha topilmadi (keyin tekshiradi)."

    await update.message.reply_text(txt, disable_web_page_preview=False)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, clean_url, trigger_price, sell_price, last_price, category, currency, alerted FROM items ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Bo‘sh. /add bilan qo‘shing.")
        return

    lines = []
    for r in rows:
        last = r["last_price"]
        curc = r["currency"] or "SAR"
        cat = r["category"] or "-"
        al = "✅" if r["alerted"] else "⏳"
        lines.append(f"#{r['id']} {al} trig={r['trigger_price']} last={last if last is not None else '-'} {curc} cat={cat}\n{r['clean_url']}")
    await update.message.reply_text("\n\n".join(lines), disable_web_page_preview=False)

async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("❌ /item <id>")
        return
    try:
        item_id = int(parts[1])
    except Exception:
        await update.message.reply_text("❌ id raqam bo‘lsin")
        return

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE id=?", (item_id,))
    r = cur.fetchone()
    conn.close()

    if not r:
        await update.message.reply_text("Topilmadi.")
        return

    txt = (
        f"#{r['id']}\n"
        f"URL: {r['clean_url']}\n"
        f"Title: {r['title'] or '-'}\n"
        f"Cat: {r['category'] or '-'}\n"
        f"Trigger: {r['trigger_price']}\n"
        f"Sell: {r['sell_price'] if r['sell_price'] is not None else '-'}\n"
        f"Last: {r['last_price'] if r['last_price'] is not None else '-'} {r['currency'] or 'SAR'}\n"
        f"Alerted: {r['alerted']}\n"
    )
    await update.message.reply_text(txt, disable_web_page_preview=False)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("❌ /del <id>")
        return
    try:
        item_id = int(parts[1])
    except Exception:
        await update.message.reply_text("❌ id raqam bo‘lsin")
        return

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM items WHERE id=?", (item_id,))
    n = cur.rowcount
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ O‘chirildi." if n else "Topilmadi.")

async def cmd_checkall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Tekshiryapman...")
    n, alerts = await check_all_items(context.application, manual_chat_id=update.effective_chat.id)
    await update.message.reply_text(f"✅ Tekshirildi: {n} ta. Alert: {alerts} ta.")

# =========================
# CHECK LOOP
# =========================
def load_items() -> List[sqlite3.Row]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def update_item_price(item_id: int, price: Optional[float], currency: Optional[str]):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE items
        SET last_price=?, currency=?, last_checked_ts=?
        WHERE id=?
    """, (price, currency, int(time.time()), item_id))
    conn.commit()
    conn.close()

def mark_alerted(item_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE items SET alerted=1 WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

async def send_alert(app: Application, text: str, chat_id: int):
    await app.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=False)

async def check_one(app: Application, row: sqlite3.Row) -> Tuple[bool, str]:
    """
    Returns: (alerted_now, debug_msg)
    """
    item_id = row["id"]
    clean_url = row["clean_url"]
    trigger = float(row["trigger_price"])
    alerted = int(row["alerted"] or 0)
    title = row["title"] or ""

    # HTML fetch in thread
    try:
        html = await asyncio.to_thread(fetch_html, clean_url)
        t, price, currency = extract_title_price(html)
        if t and not title:
            # title DBga yozish (optional)
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("UPDATE items SET title=? WHERE id=?", (t, item_id))
            conn.commit()
            conn.close()
            title = t

        if price is None:
            update_item_price(item_id, None, currency or row["currency"])
            return False, f"#{item_id} price not found"

        update_item_price(item_id, float(price), currency)

        # Trigger: price <= trigger
        if alerted == 0 and float(price) <= trigger:
            mark_alerted(item_id)
            msg = f"🔥 CHEGIRMA!\n#{item_id} {title}\n💰 {price} {currency}\n🎯 trigger={trigger}\n🔗 {clean_url}"
            return True, msg

        return False, f"#{item_id} ok {price} {currency}"

    except Exception as e:
        return False, f"#{item_id} error: {e}"

async def check_all_items(app: Application, manual_chat_id: Optional[int] = None) -> Tuple[int, int]:
    rows = load_items()
    alerts = 0

    for r in rows:
        alerted_now, msg = await check_one(app, r)
        if alerted_now:
            alerts += 1
            # kimga yuborish:
            # 1) manual /checkall bo'lsa shu chatga
            # 2) default admin (ADMIN_ID)
            target = manual_chat_id or (ADMIN_ID if ADMIN_ID else None)
            if target:
                await send_alert(app, msg, target)
            # channelga ham yuborish ixtiyoriy
            if AUTO_POST_TO_CHANNEL and CHANNEL_ID:
                try:
                    await send_alert(app, msg, CHANNEL_ID)
                except Exception:
                    pass

        # kichkina pauza: saytlar bloklamasin
        await asyncio.sleep(1.2)

    return len(rows), alerts

async def background_loop(app: Application):
    # bot start bo'lgandan keyin doimiy tekshiruv
    while True:
        try:
            await check_all_items(app)
        except Exception:
            pass
        await asyncio.sleep(max(30, CHECK_EVERY_SEC))

async def post_init(app: Application):
    # PTB JobQueue kerak emas
    app.create_task(background_loop(app))

# =========================
# MAIN
# =========================
def main():
    init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("checkall", cmd_checkall))

    print("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
