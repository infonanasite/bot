import sys
import traceback
import re
import html
import random
import string
import secrets

try:
    import os
    import logging
    import sqlite3
    from datetime import datetime
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
    from telegram.error import TelegramError, NetworkError, TimedOut
except ImportError as e:
    print(f"❌ MISSING LIBRARY: {e}")
    print("\nPlease install required library:")
    print("pip install python-telegram-bot>=21.0")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ========== CONFIGURATION ==========
BOT_TOKEN = "8565723558:AAGCVEmD9Z92JBnSPvc2VES9vwCIulxZnCU"
OWNER_ID = 0  # ⚠️ REPLACE WITH YOUR TELEGRAM ID (get from @userinfobot)
ADMIN_IDS = set()  # Will be loaded from database, owner can add/remove
BOT_USERNAME = "nyxvectorXbot"
LOG_CHANNEL = "@NyxVectorBackup"  # Make sure bot is a member

# Referral reward threshold
REFERRAL_REWARD_COUNT = 5

# Code generation settings
CODE_LENGTH_MIN = 8
CODE_LENGTH_MAX = 12
CODE_CHARS = string.ascii_uppercase + string.digits

# Global dictionaries
awaiting_screenshot = {}
awaiting_netflix_data = {}
awaiting_delete_all = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== RANDOM CODE GENERATION ==========
def generate_random_code():
    length = random.randint(CODE_LENGTH_MIN, CODE_LENGTH_MAX)
    while True:
        code = ''.join(random.choices(CODE_CHARS, k=length))
        conn = None
        try:
            conn = sqlite3.connect("giveaway.db")
            c = conn.cursor()
            c.execute("SELECT 1 FROM codes WHERE code = ?", (code,))
            if not c.fetchone():
                return code
        except sqlite3.Error as e:
            logger.error(f"Error checking code uniqueness: {e}")
            return code
        finally:
            if conn:
                conn.close()

# ========== DATABASE FUNCTIONS ==========
def init_db():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        
        # Users table - create if not exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                referrer_id INTEGER DEFAULT NULL,
                referral_code TEXT,
                referral_count INTEGER DEFAULT 0
            )
        """)
        
        # Add missing columns (without UNIQUE constraint)
        c.execute("PRAGMA table_info(users)")
        existing_cols = {col[1] for col in c.fetchall()}
        
        if 'referral_code' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        if 'referral_count' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0")
        if 'referrer_id' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL")
        
        # Generate unique referral codes for any user that doesn't have one
        c.execute("SELECT user_id FROM users WHERE referral_code IS NULL")
        null_users = c.fetchall()
        for (uid,) in null_users:
            new_code = secrets.token_urlsafe(6)[:8]
            while True:
                c.execute("SELECT 1 FROM users WHERE referral_code = ?", (new_code,))
                if not c.fetchone():
                    break
                new_code = secrets.token_urlsafe(6)[:8]
            c.execute("UPDATE users SET referral_code = ? WHERE user_id = ?", (new_code, uid))
        
        # Create unique index (safe even if column already exists)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON users(referral_code)")
        
        # Codes table
        c.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                message TEXT DEFAULT '',
                is_used INTEGER DEFAULT 0,
                used_by INTEGER DEFAULT NULL,
                used_at TIMESTAMP DEFAULT NULL
            )
        """)
        c.execute("PRAGMA table_info(codes)")
        code_cols = {col[1] for col in c.fetchall()}
        if 'message' not in code_cols:
            c.execute("ALTER TABLE codes ADD COLUMN message TEXT DEFAULT ''")
        
        # Redemptions table
        c.execute("""
            CREATE TABLE IF NOT EXISTS redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                code TEXT,
                prize_message TEXT,
                redeemed_at TIMESTAMP,
                screenshot_sent INTEGER DEFAULT 0,
                screenshot_file_id TEXT,
                submitted_at TIMESTAMP
            )
        """)
        
        # Required channels table
        c.execute("""
            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE
            )
        """)
        
        # Admins table
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        
        conn.commit()
        print("✅ Database initialized")
        return True
    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def load_admins():
    global ADMIN_IDS
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins")
        ADMIN_IDS = {row[0] for row in c.fetchall()}
        # Always ensure owner is admin
        if OWNER_ID:
            ADMIN_IDS.add(OWNER_ID)
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to load admins: {e}")
    finally:
        if conn:
            conn.close()

def add_admin(user_id):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()
        ADMIN_IDS.add(user_id)
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to add admin: {e}")
        return False
    finally:
        if conn:
            conn.close()

def remove_admin(user_id):
    if user_id == OWNER_ID:
        return False
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        ADMIN_IDS.discard(user_id)
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to remove admin: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_all_admins():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins")
        return [row[0] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Failed to get admins: {e}")
        return []
    finally:
        if conn:
            conn.close()

def register_user(user_id, username, first_name, last_name, referrer_code=None):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT user_id, referral_code FROM users WHERE user_id = ?", (user_id,))
        existing = c.fetchone()
        if existing:
            return existing[1]
        
        ref_code = secrets.token_urlsafe(6)[:8]
        while True:
            c.execute("SELECT 1 FROM users WHERE referral_code = ?", (ref_code,))
            if not c.fetchone():
                break
            ref_code = secrets.token_urlsafe(6)[:8]
        
        referrer_id = None
        if referrer_code:
            c.execute("SELECT user_id FROM users WHERE referral_code = ?", (referrer_code,))
            row = c.fetchone()
            if row:
                referrer_id = row[0]
                c.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (referrer_id,))
                c.execute("SELECT referral_count FROM users WHERE user_id = ?", (referrer_id,))
                count = c.fetchone()[0]
                if count >= REFERRAL_REWARD_COUNT:
                    reward_code, reward_msg = get_random_unused_code()
                    if reward_code:
                        redeem_code(reward_code, referrer_id)
                        logger.info(f"User {referrer_id} reached {REFERRAL_REWARD_COUNT} referrals and got code {reward_code}")
        
        c.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, referrer_id, referral_code, referral_count)
            VALUES (?, ?, ?, ?, ?, ?, 0)
        """, (user_id, username, first_name, last_name, referrer_id, ref_code))
        conn.commit()
        return ref_code
    except sqlite3.Error as e:
        logger.error(f"Failed to register user {user_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_referral_info(user_id):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT referral_code, referral_count FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            return row[0], row[1]
        return None, 0
    except sqlite3.Error as e:
        logger.error(f"Failed to get referral info: {e}")
        return None, 0
    finally:
        if conn:
            conn.close()

def add_code_to_db(code, message=""):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("INSERT INTO codes (code, message) VALUES (?, ?)", (code, message))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error as e:
        logger.error(f"DB error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def delete_code_from_db(code):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("DELETE FROM codes WHERE code = ?", (code,))
        conn.commit()
        return c.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Delete error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def delete_all_codes():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("DELETE FROM codes")
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Delete all error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_required_channels():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT channel_username FROM required_channels ORDER BY id")
        return [row[0] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Failed to get channels: {e}")
        return []
    finally:
        if conn:
            conn.close()

def set_required_channels(channels):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("DELETE FROM required_channels")
        for ch in channels:
            if ch:
                c.execute("INSERT INTO required_channels (channel_username) VALUES (?)", (ch,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to set channels: {e}")
        return False
    finally:
        if conn:
            conn.close()

def parse_netflix_accounts(text):
    blocks = re.split(r'-{30,}', text)
    accounts = [block.strip() for block in blocks if block.strip()]
    accounts = [acc for acc in accounts if len(acc) > 100]
    return accounts

def add_bulk_netflix_accounts(accounts_text):
    accounts = parse_netflix_accounts(accounts_text)
    if not accounts:
        return 0, 0, [], []
    added = 0
    duplicate = 0
    added_codes = []
    for account_text in accounts:
        code = generate_random_code()
        if add_code_to_db(code, account_text):
            added += 1
            added_codes.append(code)
        else:
            duplicate += 1
    return added, duplicate, added_codes, []

def redeem_code(code, user_id):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT is_used, message FROM codes WHERE code = ?", (code,))
        row = c.fetchone()
        if not row:
            return "invalid", None
        if row[0] == 1:
            return "already_used", None
        c.execute("""
            UPDATE codes SET is_used = 1, used_by = ?, used_at = ?
            WHERE code = ?
        """, (user_id, datetime.now(), code))
        conn.commit()
        return "success", row[1]
    except sqlite3.Error as e:
        logger.error(f"Redeem error: {e}")
        return "error", None
    finally:
        if conn:
            conn.close()

def get_random_unused_code():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT code, message FROM codes WHERE is_used = 0 LIMIT 1")
        row = c.fetchone()
        if row:
            return row[0], row[1]
        return None, None
    except sqlite3.Error as e:
        logger.error(f"Get random code error: {e}")
        return None, None
    finally:
        if conn:
            conn.close()

def check_code_status(code):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT is_used FROM codes WHERE code = ?", (code,))
        row = c.fetchone()
        if not row:
            return "invalid"
        return "used" if row[0] == 1 else "valid"
    except sqlite3.Error as e:
        logger.error(f"Check error: {e}")
        return "error"
    finally:
        if conn:
            conn.close()

def get_all_users():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        return [row[0] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch users: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_all_codes():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT code, message, is_used FROM codes ORDER BY is_used, code")
        return c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch codes: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_unused_codes():
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT code FROM codes WHERE is_used = 0 ORDER BY code")
        return [row[0] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch unused codes: {e}")
        return []
    finally:
        if conn:
            conn.close()

def save_screenshot_record(user_id, code, prize_msg, file_id):
    conn = None
    try:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO redemptions (user_id, code, prize_message, redeemed_at, screenshot_sent, screenshot_file_id, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, code, prize_msg, datetime.now(), 1, file_id, datetime.now()))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to save screenshot record: {e}")
    finally:
        if conn:
            conn.close()

# ========== CHANNEL MEMBERSHIP CHECK ==========
async def check_membership(user_id, context):
    channels = get_required_channels()
    if not channels:
        return True, []
    not_joined = []
    for ch in channels:
        try:
            clean_ch = ch.lstrip('@')
            chat = await context.bot.get_chat(f"@{clean_ch}")
            member = await context.bot.get_chat_member(chat.id, user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch)
        except Exception as e:
            logger.error(f"Error checking channel {ch}: {e}")
            not_joined.append(ch)
    return len(not_joined) == 0, not_joined

async def send_join_required(update: Update, context: ContextTypes.DEFAULT_TYPE, action, code=None):
    channels = get_required_channels()
    if not channels:
        return True
    
    not_joined = []
    for ch in channels:
        try:
            clean_ch = ch.lstrip('@')
            chat = await context.bot.get_chat(f"@{clean_ch}")
            member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    
    if not not_joined:
        return True
    
    text = "❌ <b>To continue, you must join the following channels:</b>\n\n"
    buttons = []
    for ch in not_joined:
        clean = ch.lstrip('@')
        text += f"🔹 <a href='https://t.me/{clean}'>{ch}</a>\n"
        buttons.append([InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{clean}")])
    text += "\nAfter joining, click the button below to proceed."
    
    callback_data = f"check_join_{action}"
    if code:
        callback_data += f"_{code}"
    buttons.append([InlineKeyboardButton("✅ I have joined", callback_data=callback_data)])
    
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(buttons))
    return False

# ========== LOGGING TO CHANNEL ==========
async def log_to_channel(context, text, photo_file_id=None, parse_mode=ParseMode.HTML):
    try:
        if photo_file_id:
            await context.bot.send_photo(chat_id=LOG_CHANNEL, photo=photo_file_id, caption=text, parse_mode=parse_mode)
        else:
            await context.bot.send_message(chat_id=LOG_CHANNEL, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Failed to log to channel {LOG_CHANNEL}: {e}")

# ========== ADMIN CHECK ==========
def is_admin(user_id):
    return user_id in ADMIN_IDS

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is for admin only.")
            return
        return await func(update, context)
    return wrapper

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ This command is for the bot owner only.")
            return
        return await func(update, context)
    return wrapper

# ========== COMMAND HANDLERS ==========
# ----- Public commands -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer_code = args[0] if args else None
    ref_code = register_user(user.id, user.username, user.first_name, user.last_name, referrer_code)
    
    if referrer_code:
        conn = sqlite3.connect("giveaway.db")
        c = conn.cursor()
        c.execute("SELECT referral_count, user_id FROM users WHERE referral_code = ?", (referrer_code,))
        row = c.fetchone()
        if row:
            count, referrer_id = row
            if count >= REFERRAL_REWARD_COUNT:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 Congratulations! You've reached {REFERRAL_REWARD_COUNT} referrals and received a free code! Check your account."
                )
        conn.close()
    
    await update.message.reply_html(
        f"🎁 <b>Welcome to the Giveaway Bot!</b>\n\n"
        f"🤖 Bot: @{BOT_USERNAME}\n\n"
        "📌 <b>Commands:</b>\n"
        "• /redeem &lt;code&gt; – claim a specific gift\n"
        "• /getcode – get a random available code (auto-redeem)\n"
        "• /check &lt;code&gt; – check if a code is valid\n"
        "• /ref – get your referral link\n"
        "• /referrals – check your referral progress\n\n"
        "📸 After redeeming, send a screenshot of the prize.\n\n"
        "⚠️ <b>Note:</b> You must either join all required channels OR refer 5 friends to get a free account."
    )

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name, user.last_name, None)
    ref_code, count = get_user_referral_info(user.id)
    if not ref_code:
        await update.message.reply_text("❌ Could not generate referral link. Please try again.")
        return
    needed = max(0, REFERRAL_REWARD_COUNT - count)
    link = f"https://t.me/{BOT_USERNAME}?start={ref_code}"
    await update.message.reply_html(
        f"🔗 <b>Your Referral Link</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Referrals: {count} / {REFERRAL_REWARD_COUNT}\n"
        f"🎁 Need {needed} more to get a free account!\n\n"
        f"Share this link with friends. When they join via your link, you'll get +1 referral."
    )

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name, user.last_name, None)
    ref_code, count = get_user_referral_info(user.id)
    needed = max(0, REFERRAL_REWARD_COUNT - count)
    await update.message.reply_html(
        f"📊 <b>Your Referral Stats</b>\n\n"
        f"👥 Total referrals: {count}\n"
        f"🎯 Needed for reward: {needed}\n"
        f"🏆 Reward: A free premium account code!\n\n"
        f"Use /ref to get your referral link."
    )

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    register_user(user_id, user.username, user.first_name, user.last_name, None)

    if not context.args:
        await update.message.reply_text("❌ Usage: /redeem YOUR_CODE")
        return
    code = context.args[0].strip()
    if len(code) > 100:
        await update.message.reply_text("❌ Code too long.")
        return

    # Check channel membership OR referral requirement
    channels = get_required_channels()
    is_member, not_joined = await check_membership(user_id, context)
    if not is_member:
        _, count = get_user_referral_info(user_id)
        if count < REFERRAL_REWARD_COUNT:
            await send_join_required(update, context, "redeem", code)
            return

    result, prize_msg = redeem_code(code, user_id)
    if result == "invalid":
        await update.message.reply_text("❌ Invalid code.")
    elif result == "already_used":
        await update.message.reply_text("⚠️ Code already used.")
    elif result == "error":
        await update.message.reply_text("❌ Database error. Try later.")
    else:
        reply = "✅ <b>Code redeemed successfully!</b>\n\n"
        if prize_msg:
            reply += f"🎁 <b>Prize:</b>\n<pre>{html.escape(prize_msg)}</pre>\n\n"
        reply += "📸 Please send a screenshot (photo) of the prize you received as proof.\n"
        reply += "Just send the image here. Thank you!"
        await update.message.reply_html(reply)

        awaiting_screenshot[user_id] = {"code": code, "prize": prize_msg}

        log_text = (
            f"🎁 <b>New Redemption</b>\n"
            f"👤 User: {html.escape(user.first_name)} (@{user.username or 'no username'}) (ID: {user_id})\n"
            f"🔑 Code: <code>{html.escape(code)}</code>\n"
            f"📝 Prize preview: {html.escape(prize_msg[:200] if prize_msg else 'No message')}\n"
            f"🕒 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📌 Method: /redeem"
        )
        await log_to_channel(context, log_text)

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🎁 *New Redemption*\nUser: {user.first_name} (@{user.username or 'no username'}) (ID: {user_id})\nCode: `{code}`\nPrize preview: {prize_msg[:200] if prize_msg else 'No message'}\nAwaiting screenshot...",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

async def getcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    register_user(user_id, user.username, user.first_name, user.last_name, None)

    channels = get_required_channels()
    is_member, not_joined = await check_membership(user_id, context)
    if not is_member:
        _, count = get_user_referral_info(user_id)
        if count < REFERRAL_REWARD_COUNT:
            await send_join_required(update, context, "getcode")
            return

    code, prize_msg = get_random_unused_code()
    if not code:
        await update.message.reply_text("❌ No codes available right now. Please try again later.")
        return

    result, _ = redeem_code(code, user_id)
    if result != "success":
        await update.message.reply_text("❌ Failed to redeem the code. Please try again.")
        return

    reply = "🎉 <b>Here's your code!</b>\n\n"
    reply += f"✅ Code: <code>{html.escape(code)}</code>\n\n"
    if prize_msg:
        reply += f"🎁 <b>Prize:</b>\n<pre>{html.escape(prize_msg)}</pre>\n\n"
    reply += "📸 Please send a screenshot (photo) of the prize you received as proof.\n"
    reply += "Just send the image here. Thank you!"
    await update.message.reply_html(reply)

    awaiting_screenshot[user_id] = {"code": code, "prize": prize_msg}

    log_text = (
        f"🎁 <b>New Redemption (Auto /getcode)</b>\n"
        f"👤 User: {html.escape(user.first_name)} (@{user.username or 'no username'}) (ID: {user_id})\n"
        f"🔑 Code: <code>{html.escape(code)}</code>\n"
        f"📝 Prize preview: {html.escape(prize_msg[:200] if prize_msg else 'No message')}\n"
        f"🕒 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📌 Method: /getcode"
    )
    await log_to_channel(context, log_text)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🎁 *New Redemption (auto-getcode)*\nUser: {user.first_name} (@{user.username or 'no username'}) (ID: {user_id})\nCode: `{code}`\nPrize preview: {prize_msg[:200] if prize_msg else 'No message'}\nAwaiting screenshot...",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name, user.last_name, None)

    if not context.args:
        await update.message.reply_text("❌ Usage: /check CODE")
        return
    code = context.args[0].strip()
    status = check_code_status(code)
    if status == "invalid":
        await update.message.reply_text(f"❌ Code <code>{html.escape(code)}</code> does not exist.", parse_mode=ParseMode.HTML)
    elif status == "used":
        await update.message.reply_text(f"⚠️ Code <code>{html.escape(code)}</code> has already been used.", parse_mode=ParseMode.HTML)
    elif status == "valid":
        await update.message.reply_text(f"✅ Code <code>{html.escape(code)}</code> is valid and available!", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Error checking code. Please try again later.")

# ----- Admin management (owner only) -----
@owner_only
async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /addadmin USER_ID")
        return
    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return
    if add_admin(new_admin_id):
        await update.message.reply_text(f"✅ User `{new_admin_id}` is now an admin.")
    else:
        await update.message.reply_text("❌ Failed to add admin.")

@owner_only
async def removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /removeadmin USER_ID")
        return
    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if remove_admin(admin_id):
        await update.message.reply_text(f"✅ User `{admin_id}` is no longer an admin.")
    else:
        await update.message.reply_text("❌ Failed to remove admin (maybe it's the owner).")

@owner_only
async def listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("ℹ️ No admins except owner.")
    else:
        text = "👥 <b>Current admins:</b>\n" + "\n".join(f"• `{uid}`" for uid in admins)
        await update.message.reply_html(text)

# ----- Admin commands (channel management, code management) -----
@admin_only
async def setchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "❌ <b>Usage:</b> <code>/setchannels @channel1 @channel2 ...</code>\n\n"
            "Provide up to 10 channel usernames (with @).\n"
            "Example: <code>/setchannels @channel1 @channel2</code>\n\n"
            "<b>Important:</b> The bot must be a member of each channel to verify membership."
        )
        return
    channels = [ch.strip() for ch in context.args if ch.strip()]
    if len(channels) > 10:
        await update.message.reply_text("❌ Maximum 10 channels allowed.")
        return
    
    bot_id = (await context.bot.get_me()).id
    invalid = []
    for ch in channels:
        clean = ch.lstrip('@')
        try:
            chat = await context.bot.get_chat(f"@{clean}")
            member = await context.bot.get_chat_member(chat.id, bot_id)
            if member.status in ['left', 'kicked']:
                invalid.append(ch)
        except Exception:
            invalid.append(ch)
    
    if invalid:
        await update.message.reply_html(
            f"⚠️ The bot is not a member of these channels:\n" + "\n".join(f"• {ch}" for ch in invalid) +
            "\n\nPlease add the bot to these channels first, then try again.\n"
            "For private channels, add the bot as an admin."
        )
        return
    
    if set_required_channels(channels):
        if channels:
            await update.message.reply_html(f"✅ Required channels set:\n" + "\n".join(f"• {ch}" for ch in channels))
        else:
            await update.message.reply_text("✅ Required channels cleared (no membership checks).")
    else:
        await update.message.reply_text("❌ Failed to save channels.")

@admin_only
async def viewchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = get_required_channels()
    if not channels:
        await update.message.reply_text("ℹ️ No required channels set. Anyone can redeem.")
    else:
        await update.message.reply_html("📢 <b>Required channels:</b>\n" + "\n".join(f"• {ch}" for ch in channels))

@admin_only
async def addcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /addcode CODE [message]")
        return
    code = context.args[0].strip()
    message = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    if add_code_to_db(code, message):
        if message:
            await update.message.reply_html(f"✅ Code <code>{html.escape(code)}</code> added with message:\n<pre>{html.escape(message)}</pre>")
        else:
            await update.message.reply_html(f"✅ Code <code>{html.escape(code)}</code> added.")
    else:
        await update.message.reply_html(f"⚠️ Code <code>{html.escape(code)}</code> already exists.")

@admin_only
async def addbulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "❌ <b>Usage:</b> <code>/addbulk CODE1:message CODE2:message2 CODE3</code>\n\n"
            "Example:\n<code>/addbulk ABC123:Free coffee DEF456:10% off GIFT789</code>"
        )
        return

    added = 0
    duplicate = 0
    results = []

    for item in context.args:
        item = item.strip()
        if not item:
            continue
        if ':' in item:
            parts = item.split(':', 1)
            code = parts[0].strip()
            message = parts[1].strip()
        else:
            code = item
            message = ""

        if add_code_to_db(code, message):
            added += 1
            results.append(f"✅ <code>{code}</code>")
        else:
            duplicate += 1
            results.append(f"⚠️ <code>{code}</code> (duplicate)")

    summary = f"📊 <b>Bulk add complete</b>\nAdded: {added}\nDuplicate: {duplicate}\n\n"
    if results:
        display = "\n".join(results[:15])
        if len(results) > 15:
            display += f"\n... and {len(results)-15} more"
        summary += display
    await update.message.reply_html(summary)

@admin_only
async def addnetflix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting_netflix_data[update.effective_user.id] = True
    await update.message.reply_html(
        "📺 <b>Netflix Bulk Import (Text)</b>\n\n"
        "Please send the Netflix account details in a single message.\n"
        "The bot will automatically detect each account (separated by <code>----------------------------------------</code>) and create <b>random codes</b> (8-12 chars).\n\n"
        "Each account's full details will be stored as the prize message.\n\n"
        "Send the text now (or /cancel to abort)."
    )

@admin_only
async def addbulktxt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📁 <b>Bulk import from .txt file</b>\n\n"
        "Please send a <code>.txt</code> file containing the Netflix accounts (separated by <code>----------------------------------------</code>).\n"
        "The bot will read the file and add <b>random codes</b> (8-12 chars) automatically.\n\n"
        "Send the file now (or /cancel to abort)."
    )

@admin_only
async def delcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /delcode CODE")
        return
    code = context.args[0].strip()
    if delete_code_from_db(code):
        await update.message.reply_html(f"✅ Code <code>{html.escape(code)}</code> has been deleted.")
    else:
        await update.message.reply_html(f"❌ Code <code>{html.escape(code)}</code> not found.")

@admin_only
async def delall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting_delete_all[update.effective_user.id] = True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ YES, delete ALL", callback_data="delall_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="delall_cancel")]
    ])
    await update.message.reply_html(
        "⚠️ <b>WARNING:</b> You are about to delete <b>ALL</b> codes (both used and unused).\n"
        "This action cannot be undone.\n\n"
        "Are you sure?",
        reply_markup=keyboard
    )

async def delall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ Unauthorized.")
        return
    if user_id not in awaiting_delete_all:
        await query.edit_message_text("No pending delete request.")
        return
    del awaiting_delete_all[user_id]

    if query.data == "delall_confirm":
        if delete_all_codes():
            await query.edit_message_text("✅ All codes have been deleted.")
        else:
            await query.edit_message_text("❌ Failed to delete codes. Check database.")
    else:
        await query.edit_message_text("❌ Deletion cancelled.")

@admin_only
async def managecodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codes = get_all_codes()
    if not codes:
        await update.message.reply_text("📭 No codes in the database.")
        return
    context.user_data['code_list'] = codes
    context.user_data['code_page'] = 0
    await send_code_page(update, context)

async def send_code_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    codes = context.user_data.get('code_list', [])
    page = context.user_data.get('code_page', 0)
    items_per_page = 10
    total_pages = (len(codes) + items_per_page - 1) // items_per_page
    start = page * items_per_page
    end = start + items_per_page
    page_codes = codes[start:end]

    text = f"📋 <b>Manage Codes (Page {page+1}/{total_pages})</b>\n\n"
    for idx, (code, msg, used) in enumerate(page_codes, start=start+1):
        status = "✅ Used" if used else "🆕 Available"
        text += f"{idx}. <code>{html.escape(code)}</code> – {status}\n"
        if msg:
            preview = msg.replace('\n', ' ')[:40]
            if len(msg) > 40:
                preview += "..."
            text += f"   📝 {html.escape(preview)}\n"

    keyboard = []
    row = []
    for code, _, _ in page_codes:
        row.append(InlineKeyboardButton(f"❌ {code}", callback_data=f"delcode_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data="code_prev"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data="code_next"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="code_close")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if edit:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(text, reply_markup=reply_markup)

async def codes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "code_prev":
        context.user_data['code_page'] = max(0, context.user_data.get('code_page', 0) - 1)
        await send_code_page(update, context, edit=True)
    elif data == "code_next":
        context.user_data['code_page'] = context.user_data.get('code_page', 0) + 1
        await send_code_page(update, context, edit=True)
    elif data == "code_close":
        await query.edit_message_text("✅ Management closed.")
    elif data.startswith("delcode_"):
        code = data.replace("delcode_", "")
        if delete_code_from_db(code):
            codes = get_all_codes()
            context.user_data['code_list'] = codes
            items_per_page = 10
            total_pages = (len(codes) + items_per_page - 1) // items_per_page
            current_page = context.user_data.get('code_page', 0)
            if current_page >= total_pages and total_pages > 0:
                context.user_data['code_page'] = total_pages - 1
            await send_code_page(update, context, edit=True)
        else:
            await query.edit_message_html(f"❌ Failed to delete <code>{code}</code>. It may not exist.")

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    if data.startswith("check_join_"):
        parts = data.split("_")
        action = parts[2]
        code = parts[3] if len(parts) > 3 else None
        
        is_member, not_joined = await check_membership(user_id, context)
        if is_member:
            if action == "redeem" and code:
                await query.edit_message_text("✅ You have joined all channels! Please send /redeem again with your code.")
            elif action == "getcode":
                await query.edit_message_text("✅ You have joined all channels! Please send /getcode again to receive your code.")
            else:
                await query.edit_message_text("✅ You have joined all channels! Please try the command again.")
        else:
            text = "❌ <b>You still need to join the following channels:</b>\n\n"
            buttons = []
            for ch in not_joined:
                clean = ch.lstrip('@')
                text += f"🔹 <a href='https://t.me/{clean}'>{ch}</a>\n"
                buttons.append([InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{clean}")])
            text += "\nAfter joining, click the button again."
            callback_data = f"check_join_{action}"
            if code:
                callback_data += f"_{code}"
            buttons.append([InlineKeyboardButton("✅ I have joined", callback_data=callback_data)])
            new_markup = InlineKeyboardMarkup(buttons)
            if query.message.text != text or query.message.reply_markup != new_markup:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=new_markup)

@admin_only
async def codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    unused = get_unused_codes()
    if not unused:
        await update.message.reply_text("🎁 No active codes available.")
        return
    header = "🎉 <b>Available Giveaway Codes</b>\n\n"
    header += "To redeem, send:\n<code>/redeem CODE</code>\n\n"
    header += f"Bot: @{BOT_USERNAME}\n\n"
    header += "📋 <b>Codes:</b>\n"
    codes_text = "\n".join([f"<code>{c}</code>" for c in unused])
    full_message = header + codes_text
    if len(full_message) > 4000:
        chunk = header
        for c in unused:
            line = f"<code>{c}</code>\n"
            if len(chunk) + len(line) > 3800:
                await update.message.reply_html(chunk)
                chunk = "📋 <b>Continued:</b>\n" + line
            else:
                chunk += line
        if chunk:
            await update.message.reply_html(chunk)
    else:
        await update.message.reply_html(full_message)

@admin_only
async def ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    unused = get_unused_codes()
    total = len(unused)
    channels = get_required_channels()

    if total == 0:
        msg = f"🎁 <b>GIVEAWAY ANNOUNCEMENT</b>\n\n"
        msg += f"🔥 No codes available at the moment. Check back later!\n\n"
        msg += f"👉 Bot: @{BOT_USERNAME}"
    else:
        msg = f"🎁 <b>GIVEAWAY ANNOUNCEMENT</b>\n\n"
        msg += f"✅ <b>{total}</b> premium accounts are waiting for you!\n\n"
        if channels:
            msg += "🔹 <b>Required channels to join:</b>\n"
            for ch in channels:
                clean = ch.lstrip('@')
                msg += f"• <a href='https://t.me/{clean}'>{ch}</a>\n"
            msg += "\n"
        msg += f"🔹 <b>How to participate:</b>\n"
        msg += f"1. Open the bot: @{BOT_USERNAME}\n"
        msg += f"2. Use <code>/getcode</code> to receive a random code automatically\n"
        msg += f"   OR use <code>/redeem CODE</code> with a specific code\n"
        msg += f"3. Send a screenshot of your prize\n\n"
        msg += f"🎲 <b>Sample codes</b> (first 10):\n"
        for code in unused[:10]:
            msg += f"• <code>{code}</code>\n"
        if total > 10:
            msg += f"\n... and {total-10} more! Use <code>/codes</code> to see them all.\n\n"
        msg += f"⏳ First come, first served! Good luck 🍀"

    await update.message.reply_html(msg)

@admin_only
async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /announce Your message")
        return
    message = " ".join(context.args)
    users = get_all_users()
    if not users:
        await update.message.reply_text("ℹ️ No users registered.")
        return
    success, fail = 0, 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *Announcement:*\n{message}", parse_mode=ParseMode.MARKDOWN)
            success += 1
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ Sent to {success} users. Failed: {fail}")

@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("giveaway.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM codes")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM codes WHERE is_used = 1")
    used = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    conn.close()
    await update.message.reply_html(
        f"📊 <b>Stats</b>\n"
        f"• Total codes: {total}\n"
        f"• Used codes: {used}\n"
        f"• Remaining: {total-used}\n"
        f"• Registered users: {users}"
    )

@admin_only
async def listcodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codes = get_all_codes()
    if not codes:
        await update.message.reply_text("📭 No codes.")
        return
    lines = []
    for code, msg, used in codes:
        status = "✅ Used" if used else "🆕 Available"
        line = f"<code>{html.escape(code)}</code> – {status}"
        if msg:
            preview = msg.replace('\n', ' ')[:50]
            if len(msg) > 50:
                preview += "..."
            line += f"\n   📝 {html.escape(preview)}"
        lines.append(line)
    chunk_size = 15
    for i in range(0, len(lines), chunk_size):
        chunk = "\n\n".join(lines[i:i+chunk_size])
        await update.message.reply_html(f"📋 <b>All codes:</b>\n\n{chunk}")

@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in awaiting_netflix_data:
        del awaiting_netflix_data[user_id]
        await update.message.reply_text("✅ Netflix import cancelled.")
    elif user_id in awaiting_screenshot:
        del awaiting_screenshot[user_id]
        await update.message.reply_text("✅ Screenshot request cancelled.")
    elif user_id in awaiting_delete_all:
        del awaiting_delete_all[user_id]
        await update.message.reply_text("✅ Delete-all request cancelled.")
    else:
        await update.message.reply_text("No pending operation to cancel.")

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎁 <b>Giveaway Bot Help</b>\n\n"
        f"🤖 Bot: @{BOT_USERNAME}\n\n"
        "<b>Public commands:</b>\n"
        "• /start – Register yourself\n"
        "• /redeem &lt;code&gt; – Redeem a specific code (must join required channels or have 5 referrals)\n"
        "• /getcode – Get a random available code (auto-redeem)\n"
        "• /check &lt;code&gt; – Check if a code is valid/unused\n"
        "• /ref – Get your referral link\n"
        "• /referrals – Check your referral progress\n\n"
        "<b>Admin commands:</b>\n"
        "• /setchannels @ch1 @ch2 ... – Set up to 10 required channels (bot must be a member)\n"
        "• /viewchannels – Show current required channels\n"
        "• /addcode &lt;code&gt; [message]\n"
        "• /addbulk CODE1:msg CODE2:msg2 ...\n"
        "• /addnetflix – Import Netflix accounts via text (random codes)\n"
        "• /addbulktxt – Upload .txt file with Netflix accounts (random codes)\n"
        "• /delcode &lt;code&gt; – Delete a single code\n"
        "• /delall – Delete ALL codes (with confirmation)\n"
        "• /managecodes – Interactive code management with delete buttons\n"
        "• /codes – List all available codes\n"
        "• /ad – Generate advertisement message\n"
        "• /announce &lt;msg&gt; – Broadcast to all users\n"
        "• /stats – Show bot statistics\n"
        "• /listcodes – Show all codes (including used)\n"
        "• /cancel – Cancel pending import or screenshot request\n"
        "• /help – Show this message\n\n"
        "<b>Owner commands:</b>\n"
        "• /addadmin &lt;user_id&gt; – Add a new admin\n"
        "• /removeadmin &lt;user_id&gt; – Remove an admin\n"
        "• /listadmins – List all admins"
    )
    await update.message.reply_html(help_text)

# ========== MESSAGE HANDLERS ==========
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    photo = update.message.photo[-1]
    file_id = photo.file_id

    if user_id in awaiting_screenshot:
        data = awaiting_screenshot.pop(user_id)
        code = data["code"]
        prize_msg = data["prize"]

        save_screenshot_record(user_id, code, prize_msg, file_id)

        await update.message.reply_text("✅ Thank you! Your screenshot has been received. The admin will review it if needed.")

        log_text = (
            f"📸 <b>Screenshot Received</b>\n"
            f"👤 User: {html.escape(user.first_name)} (@{user.username or 'no username'}) (ID: {user_id})\n"
            f"🔑 Code: <code>{html.escape(code)}</code>\n"
            f"📝 Prize preview: {html.escape(prize_msg[:300] if prize_msg else 'No message')}\n"
            f"🕒 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await log_to_channel(context, log_text, photo_file_id=file_id)

        caption = f"📸 *Screenshot from user*\n"
        caption += f"User: {user.first_name} (@{user.username or 'no username'}) (ID: {user_id})\n"
        caption += f"Redeemed code: `{code}`\n"
        caption += f"Prize preview: {prize_msg[:300] if prize_msg else 'No message'}\n"
        caption += f"Sent at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to send screenshot to admin {admin_id}: {e}")
    else:
        await update.message.reply_text("📸 You are not expected to send a screenshot right now. If you redeemed a code, please use /redeem first.")

async def handle_netflix_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if user_id not in awaiting_netflix_data:
        return
    del awaiting_netflix_data[user_id]

    text = update.message.text
    if not text:
        await update.message.reply_text("❌ No text received. Please send the account details as text.")
        return

    added, duplicate, added_codes, _ = add_bulk_netflix_accounts(text)
    if added == 0:
        await update.message.reply_text("❌ No valid accounts found. Make sure each account block is separated by `----------------------------------------`.")
        return

    reply = f"📊 <b>Netflix Bulk Import Complete</b>\n"
    reply += f"Accounts found: {added + duplicate}\n"
    reply += f"Codes added: {added}\n"
    reply += f"Duplicates skipped: {duplicate}\n\n"
    if added_codes:
        reply += f"✅ <b>Generated codes</b> (first 10):\n"
        for code in added_codes[:10]:
            reply += f"• <code>{code}</code>\n"
        if len(added_codes) > 10:
            reply += f"... and {len(added_codes)-10} more\n"
    await update.message.reply_html(reply)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please send a `.txt` file.")
        return

    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        text = file_content.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        await update.message.reply_text("❌ Failed to read the file. Make sure it's a valid text file.")
        return

    added, duplicate, added_codes, _ = add_bulk_netflix_accounts(text)
    if added == 0:
        await update.message.reply_text("❌ No valid accounts found in the file. Make sure each account block is separated by `----------------------------------------`.")
        return

    reply = f"📊 <b>Netflix Bulk Import from File Complete</b>\n"
    reply += f"Accounts found: {added + duplicate}\n"
    reply += f"Codes added: {added}\n"
    reply += f"Duplicates skipped: {duplicate}\n\n"
    if added_codes:
        reply += f"✅ <b>Generated codes</b> (first 10):\n"
        for code in added_codes[:10]:
            reply += f"• <code>{code}</code>\n"
        if len(added_codes) > 10:
            reply += f"... and {len(added_codes)-10} more\n"
    await update.message.reply_html(reply)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ An internal error occurred. Please try again later.")
        except:
            pass

# ========== MAIN ==========
def main():
    global OWNER_ID
    # Ask for owner ID if not set
    if OWNER_ID == 0:
        print("⚠️ OWNER_ID is not set!")
        print("Please enter your Telegram numeric ID (get from @userinfobot):")
        try:
            OWNER_ID = int(input("Your ID: ").strip())
        except:
            print("Invalid ID. Exiting.")
            input("Press Enter to exit...")
            return
    load_admins()
    print("🔥 Starting BEAST EDITION Giveaway Bot 🔥")
    print(f"Owner: {OWNER_ID}")
    print(f"Admins: {', '.join(str(aid) for aid in ADMIN_IDS)}")
    print(f"Bot: @{BOT_USERNAME}")
    print(f"Log channel: {LOG_CHANNEL}")
    print(f"Referral reward: {REFERRAL_REWARD_COUNT} referrals")
    if not init_db():
        input("Press Enter to exit...")
        return
    try:
        app = Application.builder().token(BOT_TOKEN).build()
    except Exception as e:
        print(f"❌ Bot creation failed: {e}")
        input("Press Enter to exit...")
        return

    app.add_error_handler(error_handler)

    # Public commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("getcode", getcode))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("ref", ref))
    app.add_handler(CommandHandler("referrals", referrals))

    # Owner commands
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))
    app.add_handler(CommandHandler("listadmins", listadmins))

    # Admin commands
    app.add_handler(CommandHandler("setchannels", setchannels))
    app.add_handler(CommandHandler("viewchannels", viewchannels))
    app.add_handler(CommandHandler("addcode", addcode))
    app.add_handler(CommandHandler("addbulk", addbulk))
    app.add_handler(CommandHandler("addnetflix", addnetflix))
    app.add_handler(CommandHandler("addbulktxt", addbulktxt))
    app.add_handler(CommandHandler("delcode", delcode))
    app.add_handler(CommandHandler("delall", delall))
    app.add_handler(CommandHandler("managecodes", managecodes))
    app.add_handler(CommandHandler("codes", codes))
    app.add_handler(CommandHandler("ad", ad))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("listcodes", listcodes))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(codes_callback, pattern="^(code_prev|code_next|code_close|delcode_)"))
    app.add_handler(CallbackQueryHandler(delall_callback, pattern="^delall_"))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join_"))

    # Non-command message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_netflix_data))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("✅ Bot is running. Press Ctrl+C to stop.")
    try:
        app.run_polling()
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        traceback.print_exc()
        input("Press Enter to exit...")

if __name__ == "__main__":
    main()