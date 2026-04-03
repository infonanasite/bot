import os
import sys
import asyncio
import logging
import secrets
import string
import random
import re
import html
from datetime import datetime

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# Database
import aiomysql
import ssl

# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== CONFIGURATION (from environment) ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "nyxvectorXbot")
LOG_CHANNEL = os.getenv("LOG_CHANNEL", "@NyxVectorBackup")

# TiDB Cloud connection
DB_HOST = os.getenv("DB_HOST", "gateway01.eu-central-1.prod.aws.tidbcloud.com")
DB_PORT = int(os.getenv("DB_PORT", "4000"))
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "sys")

REFERRAL_REWARD_COUNT = 5

# Global state
awaiting_screenshot = {}
awaiting_netflix_data = {}
awaiting_delete_all = {}
user_states = {}
ADMIN_IDS = set()
db_pool = None

# ========== DATABASE SETUP ==========
async def init_db():
    global db_pool
    # Create SSL context for TiDB Cloud (required)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    db_pool = await aiomysql.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        autocommit=True,
        minsize=1,
        maxsize=5,
        ssl=ssl_ctx
    )
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Create tables if not exist
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    referrer_id BIGINT,
                    referral_code VARCHAR(50) UNIQUE,
                    referral_count INT DEFAULT 0
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS codes (
                    code VARCHAR(100) PRIMARY KEY,
                    message TEXT,
                    is_used INT DEFAULT 0,
                    used_by BIGINT,
                    used_at TIMESTAMP
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS redemptions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    code VARCHAR(100),
                    prize_message TEXT,
                    redeemed_at TIMESTAMP,
                    screenshot_sent INT DEFAULT 0,
                    screenshot_file_id VARCHAR(255),
                    submitted_at TIMESTAMP
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    channel_username VARCHAR(100) UNIQUE
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY
                )
            """)
    logger.info("✅ Database connected and tables ready")

async def load_admins():
    global ADMIN_IDS
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM admins")
            rows = await cur.fetchall()
            ADMIN_IDS = {row[0] for row in rows}
            if OWNER_ID:
                ADMIN_IDS.add(OWNER_ID)
                await cur.execute("INSERT IGNORE INTO admins (user_id) VALUES (%s)", (OWNER_ID,))
    logger.info(f"Admins: {ADMIN_IDS}")

# ========== SHORTENED DATABASE FUNCTIONS (keep all needed) ==========
async def register_user(user_id, username, first_name, last_name, referrer_code=None):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id, referral_code FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            if row:
                return row[1]
            ref_code = secrets.token_urlsafe(6)[:8]
            while True:
                await cur.execute("SELECT 1 FROM users WHERE referral_code = %s", (ref_code,))
                if not await cur.fetchone():
                    break
                ref_code = secrets.token_urlsafe(6)[:8]
            referrer_id = None
            if referrer_code:
                await cur.execute("SELECT user_id FROM users WHERE referral_code = %s", (referrer_code,))
                rrow = await cur.fetchone()
                if rrow:
                    referrer_id = rrow[0]
                    await cur.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = %s", (referrer_id,))
                    await cur.execute("SELECT referral_count FROM users WHERE user_id = %s", (referrer_id,))
                    cnt = (await cur.fetchone())[0]
                    if cnt >= REFERRAL_REWARD_COUNT:
                        reward_code, _ = await get_random_unused_code()
                        if reward_code:
                            await redeem_code(reward_code, referrer_id)
            await cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, referrer_id, referral_code, referral_count)
                VALUES (%s, %s, %s, %s, %s, %s, 0)
            """, (user_id, username, first_name, last_name, referrer_id, ref_code))
            return ref_code

async def get_user_referral_info(user_id):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT referral_code, referral_count FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
            return None, 0

async def add_code(code, message=""):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO codes (code, message) VALUES (%s, %s)", (code, message))
                return True
            except:
                return False

async def delete_code(code):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM codes WHERE code = %s", (code,))
            return cur.rowcount > 0

async def delete_all_codes():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM codes")
            return True

async def get_required_channels():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT channel_username FROM required_channels ORDER BY id")
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def set_required_channels(channels):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM required_channels")
            for ch in channels:
                if ch:
                    await cur.execute("INSERT INTO required_channels (channel_username) VALUES (%s)", (ch,))
            return True

async def redeem_code(code, user_id):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT is_used, message FROM codes WHERE code = %s", (code,))
            row = await cur.fetchone()
            if not row:
                return "invalid", None
            if row[0] == 1:
                return "already_used", None
            await cur.execute("UPDATE codes SET is_used = 1, used_by = %s, used_at = %s WHERE code = %s",
                              (user_id, datetime.now(), code))
            return "success", row[1]

async def get_random_unused_code():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT code, message FROM codes WHERE is_used = 0 LIMIT 1")
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
            return None, None

async def check_code_status(code):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT is_used FROM codes WHERE code = %s", (code,))
            row = await cur.fetchone()
            if not row:
                return "invalid"
            return "used" if row[0] == 1 else "valid"

async def get_all_users():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM users")
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def get_all_codes():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT code, message, is_used FROM codes ORDER BY is_used, code")
            rows = await cur.fetchall()
            return rows

async def get_unused_codes():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT code FROM codes WHERE is_used = 0 ORDER BY code")
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def save_screenshot(user_id, code, prize_msg, file_id):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO redemptions (user_id, code, prize_message, redeemed_at, screenshot_sent, screenshot_file_id, submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, code, prize_msg, datetime.now(), 1, file_id, datetime.now()))

def generate_random_code():
    chars = string.ascii_uppercase + string.digits
    length = random.randint(8, 12)
    return ''.join(random.choices(chars, k=length))

async def add_bulk_netflix(accounts_text):
    blocks = re.split(r'-{30,}', accounts_text)
    accounts = [b.strip() for b in blocks if b.strip() and len(b) > 100]
    added = 0
    dup = 0
    codes = []
    for acc in accounts:
        c = generate_random_code()
        if await add_code(c, acc):
            added += 1
            codes.append(c)
        else:
            dup += 1
    return added, dup, codes

# ========== TELEGRAM HELPERS ==========
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_main_keyboard(user_id):
    kb = [
        [InlineKeyboardButton("🎁 Redeem a Code", callback_data="redeem_prompt")],
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="ref")],
        [InlineKeyboardButton("📊 My Referral Stats", callback_data="referrals")],
        [InlineKeyboardButton("🔍 Check a Code", callback_data="check_code")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 All Codes", callback_data="admin_listcodes")],
        [InlineKeyboardButton("➕ Add Code", callback_data="admin_addcode")],
        [InlineKeyboardButton("📁 Netflix Bulk", callback_data="admin_addnetflix")],
        [InlineKeyboardButton("📂 Netflix File", callback_data="admin_addbulktxt")],
        [InlineKeyboardButton("🗑️ Delete Code", callback_data="admin_delcode")],
        [InlineKeyboardButton("💣 Delete All", callback_data="admin_delall")],
        [InlineKeyboardButton("📢 Announce", callback_data="admin_announce")],
        [InlineKeyboardButton("🔧 Set Channels", callback_data="admin_setchannels")],
        [InlineKeyboardButton("👁️ View Channels", callback_data="admin_viewchannels")],
        [InlineKeyboardButton("👥 Admins", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]
    ])

def get_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]])

# ========== HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    ref_code = args[0] if args else None
    await register_user(user.id, user.username, user.first_name, user.last_name, ref_code)
    text = f"🎁 <b>Welcome!</b>\nBot: @{BOT_USERNAME}\n\nUse the buttons below."
    await update.message.reply_html(text, reply_markup=get_main_keyboard(user.id))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data == "redeem_prompt":
        await query.edit_message_text("Send the code to redeem:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_redeem"}
    elif data == "ref":
        ref, cnt = await get_user_referral_info(uid)
        needed = max(0, REFERRAL_REWARD_COUNT - cnt)
        link = f"https://t.me/{BOT_USERNAME}?start={ref}"
        text = f"🔗 Your link:\n<code>{link}</code>\n\nReferrals: {cnt}/{REFERRAL_REWARD_COUNT}\nNeed {needed} more for a free code."
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())
    elif data == "referrals":
        _, cnt = await get_user_referral_info(uid)
        needed = max(0, REFERRAL_REWARD_COUNT - cnt)
        text = f"📊 Your referrals: {cnt}\nNeed {needed} more for a reward."
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())
    elif data == "check_code":
        await query.edit_message_text("Send the code to check:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_check"}
    elif data == "help":
        text = "🎁 Help:\n/redeem CODE\n/ref – get link\n/referrals – stats\n/check CODE\n/start – menu"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())
    elif data == "admin_panel":
        await query.edit_message_text("⚙️ Admin Panel", reply_markup=get_admin_panel_keyboard())
    elif data == "cancel":
        await query.edit_message_text("Cancelled.")
        user_states.pop(uid, None)
        await query.message.reply_text("Main menu:", reply_markup=get_main_keyboard(uid))
    elif data == "back_to_start":
        await query.edit_message_text("Main menu:", reply_markup=get_main_keyboard(uid))
    elif data.startswith("admin_"):
        await admin_button_callback(update, context)

async def admin_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = update.effective_user.id
    if not is_admin(uid):
        await query.edit_message_text("⛔ Admin only.")
        return
    if data == "admin_stats":
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM codes")
                total = (await cur.fetchone())[0]
                await cur.execute("SELECT COUNT(*) FROM codes WHERE is_used = 1")
                used = (await cur.fetchone())[0]
                await cur.execute("SELECT COUNT(*) FROM users")
                users = (await cur.fetchone())[0]
        text = f"📊 Stats\nTotal: {total}\nUsed: {used}\nRemaining: {total-used}\nUsers: {users}"
        await query.edit_message_text(text, reply_markup=get_back_button())
    elif data == "admin_listcodes":
        codes = await get_all_codes()
        if not codes:
            text = "No codes."
        else:
            lines = [f"<code>{c[0]}</code> – {'✅ Used' if c[2] else '🆕 Available'}" for c in codes[:30]]
            text = "📋 Codes:\n" + "\n".join(lines)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())
    elif data == "admin_addcode":
        await query.edit_message_text("Send: CODE [optional message]", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_addcode"}
    elif data == "admin_addnetflix":
        awaiting_netflix_data[uid] = True
        await query.edit_message_text("Send Netflix accounts (separated by --------)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
    elif data == "admin_addbulktxt":
        await query.edit_message_text("Send a .txt file with accounts.")
    elif data == "admin_delcode":
        await query.edit_message_text("Send the code to delete.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_delcode"}
    elif data == "admin_delall":
        awaiting_delete_all[uid] = True
        await query.edit_message_text("⚠️ Delete ALL codes? Confirm:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES", callback_data="delall_confirm")],
            [InlineKeyboardButton("❌ NO", callback_data="cancel")]
        ]))
    elif data == "admin_announce":
        await query.edit_message_text("Send your announcement message:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_announce"}
    elif data == "admin_setchannels":
        await query.edit_message_text("Send channel usernames with @, space separated.\nExample: @ch1 @ch2", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_setchannels"}
    elif data == "admin_viewchannels":
        chans = await get_required_channels()
        text = "Required channels:\n" + "\n".join(f"• {c}" for c in chans) if chans else "No channels set."
        await query.edit_message_text(text, reply_markup=get_back_button())
    elif data == "admin_manage_admins":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Admin", callback_data="admin_addadmin")],
            [InlineKeyboardButton("➖ Remove Admin", callback_data="admin_removeadmin")],
            [InlineKeyboardButton("📋 List Admins", callback_data="admin_listadmins")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ])
        await query.edit_message_text("Manage admins:", reply_markup=kb)
    elif data == "admin_addadmin":
        await query.edit_message_text("Send numeric user ID of new admin.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_addadmin"}
    elif data == "admin_removeadmin":
        await query.edit_message_text("Send numeric user ID to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        user_states[uid] = {"state": "waiting_removeadmin"}
    elif data == "admin_listadmins":
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM admins")
                rows = await cur.fetchall()
        text = "Admins:\n" + "\n".join(f"• `{r[0]}`" for r in rows) if rows else "Only owner."
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if uid in user_states:
        state = user_states.pop(uid)["state"]
        if state == "waiting_redeem":
            # Call redeem command
            result, prize = await redeem_code(text, uid)
            if result == "invalid":
                await update.message.reply_text("❌ Invalid code.")
            elif result == "already_used":
                await update.message.reply_text("⚠️ Code already used.")
            else:
                reply = "✅ Code redeemed!\n\n"
                if prize:
                    reply += f"🎁 Prize:\n<pre>{html.escape(prize)}</pre>\n\n"
                reply += "📸 Send a screenshot of your prize."
                await update.message.reply_html(reply)
                awaiting_screenshot[uid] = {"code": text, "prize": prize}
        elif state == "waiting_check":
            status = await check_code_status(text)
            if status == "invalid":
                await update.message.reply_html(f"❌ Code <code>{html.escape(text)}</code> does not exist.")
            elif status == "used":
                await update.message.reply_html(f"⚠️ Code <code>{html.escape(text)}</code> already used.")
            else:
                await update.message.reply_html(f"✅ Code <code>{html.escape(text)}</code> is valid!")
        elif state == "waiting_addcode":
            parts = text.split(maxsplit=1)
            code = parts[0]
            msg = parts[1] if len(parts) > 1 else ""
            if await add_code(code, msg):
                await update.message.reply_html(f"✅ Code <code>{code}</code> added.")
            else:
                await update.message.reply_html(f"⚠️ Code <code>{code}</code> already exists.")
        elif state == "waiting_delcode":
            if await delete_code(text):
                await update.message.reply_html(f"✅ Code <code>{html.escape(text)}</code> deleted.")
            else:
                await update.message.reply_html(f"❌ Code <code>{html.escape(text)}</code> not found.")
        elif state == "waiting_announce":
            users = await get_all_users()
            success = 0
            for uid2 in users:
                try:
                    await context.bot.send_message(chat_id=uid2, text=f"📢 *Announcement:*\n{text}", parse_mode=ParseMode.MARKDOWN)
                    success += 1
                except:
                    pass
            await update.message.reply_text(f"✅ Sent to {success} users.")
        elif state == "waiting_setchannels":
            channels = text.split()
            if len(channels) > 10:
                await update.message.reply_text("Max 10 channels.")
            else:
                await set_required_channels(channels)
                await update.message.reply_text(f"✅ Required channels set: {', '.join(channels)}")
        elif state == "waiting_addadmin":
            if update.effective_user.id != OWNER_ID:
                await update.message.reply_text("Only owner can add admins.")
                return
            try:
                uid2 = int(text)
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("INSERT IGNORE INTO admins (user_id) VALUES (%s)", (uid2,))
                        ADMIN_IDS.add(uid2)
                await update.message.reply_text(f"✅ User {uid2} is now admin.")
            except:
                await update.message.reply_text("Invalid ID.")
        elif state == "waiting_removeadmin":
            if update.effective_user.id != OWNER_ID:
                await update.message.reply_text("Only owner can remove admins.")
                return
            try:
                uid2 = int(text)
                if uid2 == OWNER_ID:
                    await update.message.reply_text("Cannot remove owner.")
                    return
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("DELETE FROM admins WHERE user_id = %s", (uid2,))
                        ADMIN_IDS.discard(uid2)
                await update.message.reply_text(f"✅ User {uid2} removed from admins.")
            except:
                await update.message.reply_text("Invalid ID.")
        # Show main menu after action
        await update.message.reply_text("Main menu:", reply_markup=get_main_keyboard(uid))
    else:
        await update.message.reply_text("Use /start to begin.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in awaiting_screenshot:
        data = awaiting_screenshot.pop(uid)
        code = data["code"]
        prize = data["prize"]
        file_id = update.message.photo[-1].file_id
        await save_screenshot(uid, code, prize, file_id)
        await update.message.reply_text("✅ Screenshot received. Thank you!")
        # Forward to admins and log channel
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_photo(chat_id=aid, photo=file_id, caption=f"📸 Screenshot for {code} from {update.effective_user.first_name}")
            except:
                pass
    else:
        await update.message.reply_text("You are not expected to send a screenshot. Use /redeem first.")

async def handle_netflix_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if uid not in awaiting_netflix_data:
        return
    del awaiting_netflix_data[uid]
    added, dup, codes = await add_bulk_netflix(update.message.text)
    reply = f"📊 Netflix import: {added} added, {dup} duplicates.\n"
    if codes:
        reply += "First 10 codes:\n" + "\n".join(f"• `{c}`" for c in codes[:10])
    await update.message.reply_html(reply)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    doc = update.message.document
    if not doc.file_name.endswith('.txt'):
        await update.message.reply_text("Send a .txt file.")
        return
    file = await context.bot.get_file(doc.file_id)
    content = await file.download_as_bytearray()
    text = content.decode('utf-8', errors='ignore')
    added, dup, codes = await add_bulk_netflix(text)
    reply = f"📊 File import: {added} added, {dup} duplicates.\n"
    if codes:
        reply += "First 10 codes:\n" + "\n".join(f"• `{c}`" for c in codes[:10])
    await update.message.reply_html(reply)

async def delall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_admin(uid):
        await query.edit_message_text("Unauthorized.")
        return
    if uid not in awaiting_delete_all:
        await query.edit_message_text("No pending request.")
        return
    del awaiting_delete_all[uid]
    if query.data == "delall_confirm":
        await delete_all_codes()
        await query.edit_message_text("✅ All codes deleted.")
    else:
        await query.edit_message_text("Cancelled.")
    await query.message.reply_text("Main menu:", reply_markup=get_main_keyboard(uid))

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is for future channel join verification – not needed for core but kept
    pass

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ An error occurred. Try again later.")
        except:
            pass

# ========== MAIN ==========
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        sys.exit(1)
    if not DB_USER or not DB_PASSWORD:
        logger.error("Database credentials missing")
        sys.exit(1)
    await init_db()
    await load_admins()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redeem", lambda u,c: None))  # handled by text
    app.add_handler(CommandHandler("check", lambda u,c: None))
    app.add_handler(CommandHandler("ref", lambda u,c: None))
    app.add_handler(CommandHandler("referrals", lambda u,c: None))
    app.add_handler(CommandHandler("help", lambda u,c: None))

    app.add_handler(CallbackQueryHandler(button_callback, pattern="^(redeem_prompt|ref|referrals|check_code|help|admin_panel|cancel|back_to_start)$"))
    app.add_handler(CallbackQueryHandler(admin_button_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(delall_callback, pattern="^delall_"))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_netflix_data))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot started polling")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())