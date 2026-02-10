import os
import re
import json
import time
import asyncio
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
from bs4 import BeautifulSoup

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ======================
# ENV
# ======================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()

CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC") or "900")          # 15 min
MIN_DISCOUNT_PCT = float(os.getenv("MIN_DISCOUNT_PCT") or "25")
AUTO_POST_TO_CHANNEL = (os.getenv("AUTO_POST_TO_CHANNEL") or "0").strip() == "1"

SELL_MARKUP = float(os.getenv("SELL_MARKUP") or "1.35")               # +35%
SELL_ADD = float(os.getenv("SELL_ADD") or "0")                        # SAR
SELL_ROUND = float(os.getenv("SELL_ROUND") or "1")
AUTO_UPDATE_SELL_ON_ALERT = (os.getenv("AUTO_UPDATE_SELL_ON_ALERT") or "1").strip() == "1"

SAR_PER_USD = float(os.getenv("SAR_PER_USD") or "3.70")               # 1$=3.70 SAR

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

SCRAPER_API_KEY = (os.getenv("SCRAPER_API_KEY") or "").strip()
SCRAPER_API_ENDPOINT = os.getenv("SCRAPER_API_ENDPOINT", "http://api.scraperapi.com").strip()

UA = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Mobile Safari/537.36",
).strip()


# ======================
# DB (PostgreSQL)
# ======================
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q. Railway’da PostgreSQL qo‘shing.")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT,
                    currency TEXT,
                    target_price DOUBLE PRECISION NOT NULL,
                    sell_price DOUBLE PRECISION,
                    category TEXT,
                    last_price DOUBLE PRECISION,
                    last_seen_ts BIGINT,
                    last_alert_price DOUBLE PRECISION,
                    image_url TEXT,
                    created_ts BIGINT NOT NULL
                );
                """
            )
        conn.commit()
    finally:
        conn.close()


# ======================
# UTIL
# ======================
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and (ADMIN_ID == 0 or u.id == ADMIN_ID))

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s

def clean_price(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("\u00a0", " ")
    m = re.findall(r"[0-9]+(?:[.,][0-9]+)?", s)
    if not m:
        return None
    val = m[0].replace(",", ".")
    try:
        return float(val)
    except:
        return None

def discount_pct(base: float, now: float) -> float:
    if base <= 0:
        return 0.0
    return max(0.0, (base - now) / base * 100.0)

def round_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(x / step) * step

def calc_sell_price(last_price: Optional[float]) -> Optional[float]:
    if last_price is None:
        return None
    raw = last_price * SELL_MARKUP + SELL_ADD
    return round_to_step(raw, SELL_ROUND)

def sar_to_usd(sar: Optional[float]) -> Optional[float]:
    if sar is None:
        return None
    return sar / SAR_PER_USD


# ======================
# PARSING
# ======================
def extract_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.get_text(strip=True))
            if isinstance(data, list):
                out.extend([x for x in data if isinstance(x, dict)])
            elif isinstance(data, dict):
                out.append(data)
        except:
            continue
    return out

def find_product_offer(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    title = None
    image_url = None

    for k in ("name", "headline", "title"):
        if isinstance(data.get(k), str):
            title = data.get(k)
            break

    img = data.get("image")
    if isinstance(img, str):
        image_url = img
    elif isinstance(img, list) and img and isinstance(img[0], str):
        image_url = img[0]

    offers = data.get("offers")
    price = None
    currency = None

    def parse_offer(off: Dict[str, Any]):
        p = off.get("price") or off.get("lowPrice")
        pr = clean_price(str(p)) if p is not None else None
        cur = off.get("priceCurrency") if isinstance(off.get("priceCurrency"), str) else None
        return pr, cur

    if isinstance(offers, dict):
        price, currency = parse_offer(offers)
    elif isinstance(offers, list):
        for off in offers:
            if isinstance(off, dict):
                pr, cur = parse_offer(off)
                if pr is not None:
                    price, currency = pr, cur
                    break

    return price, currency, title, image_url

def parse_page(html: str) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD Product
    for obj in extract_json_ld(soup):
        p, cur, title, img = find_product_offer(obj)
        if p is not None:
            if not title:
                title = soup.title.get_text(strip=True) if soup.title else None
            if not img:
                ogi = soup.find("meta", {"property": "og:image"})
                if ogi and ogi.get("content"):
                    img = ogi["content"]
            return p, cur, title, img

    # meta product price
    meta_price = soup.find("meta", {"property": "product:price:amount"}) or soup.find("meta", {"name": "product:price:amount"})
    if meta_price and meta_price.get("content"):
        p = clean_price(meta_price["content"])
        cur_meta = soup.find("meta", {"property": "product:price:currency"}) or soup.find("meta", {"name": "product:price:currency"})
        cur = cur_meta["content"] if cur_meta and cur_meta.get("content") else None

        title = None
        ogt = soup.find("meta", {"property": "og:title"})
        if ogt and ogt.get("content"):
            title = ogt["content"]
        elif soup.title:
            title = soup.title.get_text(strip=True)

        ogi = soup.find("meta", {"property": "og:image"})
        img = ogi["content"] if ogi and ogi.get("content") else None
        return p, cur, title, img

    # fallback SAR search
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(SAR|ر\.س)\s*([0-9]+(?:[.,][0-9]+)?)", text)
    p = clean_price(m.group(2)) if m else None
    title = soup.title.get_text(strip=True) if soup.title else None
    ogi = soup.find("meta", {"property": "og:image"})
    img = ogi["content"] if ogi and ogi.get("content") else None
    return p, "SAR" if p is not None else None, title, img


# ======================
# FETCH
# ======================
async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    if SCRAPER_API_KEY:
        proxied = f"{SCRAPER_API_ENDPOINT}?api_key={SCRAPER_API_KEY}&url={aiohttp.helpers.quote(url, safe='')}"
        async with session.get(proxied, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
            return await r.text()
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
        return await r.text()


# ======================
# CAPTION / UI
# ======================
def build_caption(
    title: str,
    url: str,
    currency: str,
    prev_price: Optional[float],
    last_price: Optional[float],
    target_price: float,
    sell_price: Optional[float],
    category: Optional[str],
) -> str:
    now = last_price
    pct = discount_pct(target_price, now) if (now is not None) else 0.0

    cat_line = f"🏷 Kategoriya: <b>{category}</b>\n" if category else ""

    prev_usd = sar_to_usd(prev_price)
    now_usd = sar_to_usd(now)
    target_usd = sar_to_usd(target_price)
    sell_usd = sar_to_usd(sell_price)

    sell_line = (
        f"💸 Sotuv narxi: <b>{fmt_money(sell_price)} SAR</b>\n"
        f"   (${fmt_money(sell_usd)})\n"
        if sell_price is not None else ""
    )

    return (
        f"🔥 <b>CHEGIRMA TOPILDI</b>\n\n"
        f"🧾 <b>{title}</b>\n"
        f"{cat_line}"
        f"📌 Avvalgi narxi: <b>{fmt_money(prev_price)} SAR</b>\n"
        f"   (${fmt_money(prev_usd)})\n"
        f"✅ Hozirgi narxi: <b>{fmt_money(now)} SAR</b>\n"
        f"   (${fmt_money(now_usd)})\n"
        f"🎯 Trigger narx: <b>{fmt_money(target_price)} SAR</b>\n"
        f"   (${fmt_money(target_usd)})\n"
        f"📉 Farq: <b>{fmt_money(pct)}%</b>\n"
        f"{sell_line}\n"
        f"📍 Madina | 📦 Tez yetkazish mumkin\n"
        f"🔗 Link: {url}\n\n"
        f"📩 Buyurtma uchun DM"
    ).strip()

def deal_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Ko‘rish / Buyurtma", url=url)]])

def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 All", callback_data="home:list:ALL"),
                InlineKeyboardButton("💊 Vitamin", callback_data="home:list:VITAMIN"),
            ],
            [
                InlineKeyboardButton("🐟 Omega", callback_data="home:list:OMEGA"),
                InlineKeyboardButton("🥤 Protein", callback_data="home:list:PROTEIN"),
            ],
            [
                InlineKeyboardButton("🦴 Collagen", callback_data="home:list:COLLAGEN"),
                InlineKeyboardButton("🔄 Tekshir all", callback_data="home:checkall"),
            ],
        ]
    )

def panel_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Narx", callback_data=f"edit:{item_id}"),
                InlineKeyboardButton("🏷 Kategoriya", callback_data=f"cat:{item_id}"),
            ],
            [
                InlineKeyboardButton("♻️ Recalc", callback_data=f"recalc:{item_id}"),
                InlineKeyboardButton("📣 Kanalga", callback_data=f"post:{item_id}"),
            ],
            [
                InlineKeyboardButton("🔄 Tekshir", callback_data=f"check:{item_id}"),
                InlineKeyboardButton("🗑 O‘chirish", callback_data=f"del:{item_id}"),
            ],
        ]
    )


# ======================
# SENDING
# ======================
async def send_deal_message(
    app: Application,
    chat_id: str | int,
    title: str,
    url: str,
    currency: str,
    prev_price: Optional[float],
    last_price: Optional[float],
    target_price: float,
    sell_price: Optional[float],
    category: Optional[str],
    image_url: Optional[str],
):
    caption = build_caption(
        title or "Chegirma",
        url,
        currency or "SAR",
        prev_price,
        last_price,
        target_price,
        sell_price,
        category,
    )
    kb = deal_keyboard(url)

    if image_url and image_url.startswith("http"):
        try:
            await app.bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    await app.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=False,
    )


# ======================
# COMMANDS
# ======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Kechirasiz, bu bot xususiy (private).")
        return
    await update.message.reply_text(
        "✅ Deal Watcher + Admin Panel (PostgreSQL) tayyor.\n\n"
        "Buyruqlar:\n"
        "• /add <url> <trigger_price> [sell_price] [category]\n"
        "• /panel\n"
        "• /item <id>\n"
        "• /checkall\n\n"
        "Misol:\n"
        "/add https://sa.iherb.com/... 120 160 VITAMIN\n"
        "Yoki:\n"
        "/add https://sa.iherb.com/... 120\n\n"
        f"USD hisob: 1$ = {SAR_PER_USD} SAR",
        disable_web_page_preview=True,
    )

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return
    await update.message.reply_text("🧩 Admin panel:", reply_markup=home_keyboard())

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Format: /add <url> <trigger_price> [sell_price] [category]")
        return

    url = args[0].strip()
    try:
        trigger_price = float(args[1].replace(",", "."))
    except:
        await update.message.reply_text("trigger_price raqam bo‘lsin. Misol: 120 yoki 120.5")
        return

    sell_price: Optional[float] = None
    category: Optional[str] = None

    if len(args) >= 3:
        try:
            sell_price = float(args[2].replace(",", "."))
        except:
            sell_price = None

    if len(args) >= 4:
        category = args[3].strip().upper()[:24]

    async with aiohttp.ClientSession() as session:
        try:
            html = await fetch_html(session, url)
            p, cur, title, img = parse_page(html)
        except Exception as e:
            await update.message.reply_text(f"❌ Sahifani o‘qishda xato: {e}")
            return

    if sell_price is None and p is not None:
        sell_price = calc_sell_price(p)

    now_ts = int(time.time())
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO items (url, title, currency, target_price, sell_price, category, last_price, last_seen_ts, last_alert_price, image_url, created_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (url) DO UPDATE SET
                    title=EXCLUDED.title,
                    currency=EXCLUDED.currency,
                    target_price=EXCLUDED.target_price,
                    sell_price=EXCLUDED.sell_price,
                    category=EXCLUDED.category,
                    last_price=EXCLUDED.last_price,
                    last_seen_ts=EXCLUDED.last_seen_ts,
                    image_url=EXCLUDED.image_url
                RETURNING id;
                """,
                (url, title, cur or "SAR", trigger_price, sell_price, category, p, now_ts, None, img, now_ts),
            )
            row = c.fetchone()
            item_id = row["id"] if row else None
        conn.commit()
    finally:
        conn.close()

    await update.message.reply_text(
        f"✅ Qo‘shildi.\n"
        f"ID: {item_id}\n"
        f"🧾 {title or '—'}\n"
        f"Kategoriya: {category or '—'}\n"
        f"Trigger: {fmt_money(trigger_price)} SAR\n"
        f"Sell: {fmt_money(sell_price)} SAR\n"
        f"Hozir: {fmt_money(p)} SAR\n"
        f"URL: {url}\n\n"
        f"🛠 Boshqarish: /item {item_id}",
        disable_web_page_preview=True,
    )

async def cmd_checkall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return
    await update.message.reply_text("🔎 Hammasini tekshiryapman…")
    await run_check(context.application, only_item_id=None, manual_chat_id=update.effective_chat.id)
    await update.message.reply_text("✅ Tekshiruv tugadi.")

async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Format: /item <id>")
        return
    try:
        iid = int(context.args[0])
    except:
        await update.message.reply_text("id raqam bo‘lsin.")
        return

    conn = db()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id,title,currency,target_price,sell_price,category,last_price,url,image_url FROM items WHERE id=%s",
                (iid,),
            )
            row = c.fetchone()
    finally:
        conn.close()

    if not row:
        await update.message.reply_text("Topilmadi.")
        return

    text = (
        f"🧾 <b>{row['title'] or '—'}</b>\n"
        f"Kategoriya: <b>{row['category'] or '—'}</b>\n"
        f"Now: <b>{fmt_money(row['last_price'])} SAR</b>\n"
        f"Trigger: <b>{fmt_money(row['target_price'])} SAR</b>\n"
        f"Sell: <b>{fmt_money(row['sell_price'])} SAR</b>\n"
        f"Rasm: {'bor' if row['image_url'] else 'yo‘q'}\n"
        f"🔗 {row['url']}\n\n"
        f"♻️ Recalc: sell = last * {SELL_MARKUP} + {SELL_ADD} (round {SELL_ROUND})"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=panel_keyboard(iid),
        disable_web_page_preview=True,
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return

    conn = db()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id,title,currency,target_price,sell_price,last_price,category,url FROM items ORDER BY id DESC LIMIT 50"
            )
            rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        await update.message.reply_text("Ro‘yxat bo‘sh. /add bilan qo‘shing.")
        return

    lines = ["📋 <b>Watchlist (50)</b>\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} [{r['category'] or '-'}] — <b>{(r['title'] or '—')[:60]}</b>\n"
            f"Now {fmt_money(r['last_price'])} SAR | Trigger {fmt_money(r['target_price'])} SAR | Sell {fmt_money(r['sell_price'])} SAR\n"
            f"{r['url']}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ======================
# CALLBACKS / EDITS
# ======================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.callback_query:
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    m = re.match(r"^home:list:(.+)$", data)
    if m:
        cat = m.group(1).strip().upper()
        conn = db()
        try:
            with conn.cursor() as c:
                if cat == "ALL":
                    c.execute(
                        "SELECT id,title,currency,target_price,sell_price,last_price,category FROM items ORDER BY id DESC LIMIT 15"
                    )
                else:
                    c.execute(
                        "SELECT id,title,currency,target_price,sell_price,last_price,category FROM items WHERE category=%s ORDER BY id DESC LIMIT 15",
                        (cat,),
                    )
                rows = c.fetchall()
        finally:
            conn.close()

        if not rows:
            await q.edit_message_text(f"Bo‘sh: {cat}. /add bilan qo‘shing.")
            return

        text_lines = [f"📋 <b>List: {cat}</b>\n"]
        for r in rows:
            text_lines.append(
                f"#{r['id']} [{r['category'] or '-'}] — <b>{(r['title'] or '—')[:60]}</b>\n"
                f"Now {fmt_money(r['last_price'])} SAR | Trigger {fmt_money(r['target_price'])} SAR | Sell {fmt_money(r['sell_price'])} SAR\n"
            )
        await q.edit_message_text("\n".join(text_lines) + "\n👉 Boshqarish: /item <id>", parse_mode=ParseMode.HTML)
        return

    if data == "home:checkall":
        await q.edit_message_text("🔄 Hammasini tekshiryapman…")
        await run_check(context.application, only_item_id=None, manual_chat_id=q.message.chat.id)
        await context.application.bot.send_message(chat_id=q.message.chat.id, text="✅ Tekshiruv tugadi.")
        return

    m2 = re.match(r"^(edit|post|check|del|recalc|cat):(\d+)$", data)
    if not m2:
        return

    action = m2.group(1)
    iid = int(m2.group(2))

    if action == "edit":
        context.user_data["edit_item_id"] = iid
        await q.message.reply_text(f"✏️ #{iid} uchun yangi SELL narx yuboring (masalan: 160).")
        return

    if action == "cat":
        context.user_data["edit_cat_item_id"] = iid
        await q.message.reply_text("🏷 Kategoriya yuboring (VITAMIN/OMEGA/PROTEIN/COLLAGEN/...)")
        return

    if action == "del":
        conn = db()
        try:
            with conn.cursor() as c:
                c.execute("DELETE FROM items WHERE id=%s", (iid,))
                deleted = c.rowcount
            conn.commit()
        finally:
            conn.close()
        await q.message.reply_text("🗑 O‘chirildi." if deleted else "Topilmadi.")
        return

    if action == "check":
        await q.message.reply_text(f"🔄 #{iid} tekshirilyapti…")
        await run_check(context.application, only_item_id=iid, manual_chat_id=q.message.chat.id)
        return

    if action == "recalc":
        conn = db()
        try:
            with conn.cursor() as c:
                c.execute("SELECT last_price FROM items WHERE id=%s", (iid,))
                row = c.fetchone()
                if not row:
                    await q.message.reply_text("Topilmadi.")
                    return
                new_sell = calc_sell_price(row["last_price"])
                c.execute("UPDATE items SET sell_price=%s WHERE id=%s", (new_sell, iid))
            conn.commit()
        finally:
            conn.close()
        await q.message.reply_text(f"♻️ #{iid} SELL qayta hisoblandi: {fmt_money(new_sell)} SAR")
        return

    if action == "post":
        if not CHANNEL_ID:
            await q.message.reply_text("⚠️ CHANNEL_ID yo‘q. Variables’da CHANNEL_ID qo‘ying.")
            return
        await q.message.reply_text(f"📣 #{iid} kanalga joylanyapti…")
        await post_item_to_channel(context.application, iid, forced=True, notify_admin_chat=q.message.chat.id)
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return

    iid = context.user_data.get("edit_item_id")
    if iid:
        txt = (update.message.text or "").strip()
        try:
            new_sell = float(txt.replace(",", "."))
        except:
            await update.message.reply_text("❌ Narx xato. Masalan: 160 yoki 160.5")
            return

        conn = db()
        try:
            with conn.cursor() as c:
                c.execute("UPDATE items SET sell_price=%s WHERE id=%s", (new_sell, iid))
            conn.commit()
        finally:
            conn.close()

        context.user_data.pop("edit_item_id", None)
        await update.message.reply_text(f"✅ #{iid} SELL yangilandi: {fmt_money(new_sell)} SAR")
        return

    iid_cat = context.user_data.get("edit_cat_item_id")
    if iid_cat:
        cat = (update.message.text or "").strip().upper()[:24]

        conn = db()
        try:
            with conn.cursor() as c:
                c.execute("UPDATE items SET category=%s WHERE id=%s", (cat, iid_cat))
            conn.commit()
        finally:
            conn.close()

        context.user_data.pop("edit_cat_item_id", None)
        await update.message.reply_text(f"✅ #{iid_cat} kategoriya yangilandi: {cat}")
        return


# ======================
# CHECK + POST
# ======================
async def post_item_to_channel(app: Application, item_id: int, forced: bool, notify_admin_chat: Optional[int] = None):
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id,title,currency,target_price,sell_price,category,last_price,url,image_url FROM items WHERE id=%s",
                (item_id,),
            )
            row = c.fetchone()
    finally:
        conn.close()

    if not row:
        if notify_admin_chat:
            await app.bot.send_message(chat_id=notify_admin_chat, text="Topilmadi.")
        return

    if not forced and row["last_price"] is not None:
        pct = discount_pct(row["target_price"], row["last_price"])
        if not (row["last_price"] <= row["target_price"] or pct >= MIN_DISCOUNT_PCT):
            return

    lp = row["last_price"]
    await send_deal_message(
        app=app,
        chat_id=CHANNEL_ID,
        title=row["title"] or "Chegirma",
        url=row["url"],
        currency=row["currency"] or "SAR",
        prev_price=lp,
        last_price=lp,
        target_price=row["target_price"],
        sell_price=row["sell_price"],
        category=row["category"],
        image_url=row["image_url"],
    )


async def run_check(app: Application, only_item_id: Optional[int], manual_chat_id: Optional[int] = None):
    conn = db()
    try:
        with conn.cursor() as c:
            if only_item_id is None:
                c.execute(
                    "SELECT id,url,title,currency,target_price,sell_price,category,last_price,last_alert_price,image_url FROM items"
                )
            else:
                c.execute(
                    "SELECT id,url,title,currency,target_price,sell_price,category,last_price,last_alert_price,image_url FROM items WHERE id=%s",
                    (only_item_id,),
                )
            rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        if manual_chat_id:
            await app.bot.send_message(chat_id=manual_chat_id, text="Ro‘yxat bo‘sh.")
        return

    async with aiohttp.ClientSession() as session:
        for r in rows:
            iid = r["id"]
            url = r["url"]
            title = r["title"]
            cur = r["currency"] or "SAR"
            tp = r["target_price"]
            sp = r["sell_price"]
            cat = r["category"]
            prev_price = r["last_price"]
            last_alert_price = r["last_alert_price"]
            stored_img = r["image_url"]

            try:
                html = await fetch_html(session, url)
                now_price, currency, new_title, img = parse_page(html)
                if currency:
                    cur = currency
                if new_title:
                    title = new_title
                if img:
                    stored_img = img
            except Exception as e:
                if manual_chat_id:
                    await app.bot.send_message(chat_id=manual_chat_id, text=f"⚠️ #{iid} o‘qilmadi: {e}")
                continue

            if AUTO_UPDATE_SELL_ON_ALERT and now_price is not None:
                if sp is None:
                    sp = calc_sell_price(now_price)

            now_ts = int(time.time())

            # update db
            conn2 = db()
            try:
                with conn2.cursor() as c2:
                    c2.execute(
                        """
                        UPDATE items
                        SET title=%s, currency=%s, last_price=%s, last_seen_ts=%s, image_url=%s, sell_price=%s
                        WHERE id=%s
                        """,
                        (title, cur, now_price, now_ts, stored_img, sp, iid),
                    )
                conn2.commit()
            finally:
                conn2.close()

            if now_price is None:
                continue

            pct = discount_pct(tp, now_price)
            should_alert = (now_price <= tp) or (pct >= MIN_DISCOUNT_PCT)

            if should_alert and (last_alert_price is None or abs(last_alert_price - now_price) > 0.01):
                # DM admin
                if ADMIN_ID:
                    await send_deal_message(
                        app=app,
                        chat_id=ADMIN_ID,
                        title=title or f"Item #{iid}",
                        url=url,
                        currency=cur,
                        prev_price=prev_price,
                        last_price=now_price,
                        target_price=tp,
                        sell_price=sp,
                        category=cat,
                        image_url=stored_img,
                    )
                    await app.bot.send_message(chat_id=ADMIN_ID, text=f"🛠 Boshqarish: /item {iid}")

                # auto post
                if AUTO_POST_TO_CHANNEL and CHANNEL_ID:
                    await send_deal_message(
                        app=app,
                        chat_id=CHANNEL_ID,
                        title=title or "Chegirma",
                        url=url,
                        currency=cur,
                        prev_price=prev_price,
                        last_price=now_price,
                        target_price=tp,
                        sell_price=sp,
                        category=cat,
                        image_url=stored_img,
                    )

                # save last_alert_price
                conn3 = db()
                try:
                    with conn3.cursor() as c3:
                        c3.execute("UPDATE items SET last_alert_price=%s WHERE id=%s", (now_price, iid))
                    conn3.commit()
                finally:
                    conn3.close()


async def scheduler(app: Application):
    while True:
        try:
            await run_check(app, only_item_id=None)
        except Exception:
            pass
        await asyncio.sleep(CHECK_EVERY_SEC)


# ======================
# MAIN
# ======================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("checkall", cmd_checkall))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # background scheduler
    app.job_queue.run_once(lambda *_: asyncio.create_task(scheduler(app)), when=1)

    print("✅ Deal Watcher Admin Bot (PostgreSQL) running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
