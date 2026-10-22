import os
import asyncio
import json
import logging
import time
from aiohttp import web

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, constants
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    InputPeerSelf, UpdateGroupCallParticipants,
    Channel, Chat as TelegramChat
)

from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
OWNER_ID      = int(os.environ.get("OWNER_ID", "0"))
API_ID        = int(os.environ.get("API_ID", "0"))
API_HASH      = os.environ.get("API_HASH", "")
ACCOUNTS_FILE = "accounts.json"
PORT          = int(os.environ.get("PORT", "10000"))   # Render দেয়

KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL", "4"))

# ─── STATES ────────────────────────────────────────────────────────────────────
MAIN_MENU, WAIT_PHONE, WAIT_CODE, WAIT_2FA, WAIT_LINK, WAIT_COUNT = range(6)

# ─── STORAGE ───────────────────────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

pending_clients = {}
active_clients  = {}
live_sessions   = {}
live_handlers   = {}
call_engines    = {}
watchdog_tasks  = {}
live_state      = {}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"), KeyboardButton("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗜𝗦𝗧")],
        [KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗢𝗡 𝗟𝗜𝗩𝗘")],
    ], resize_keyboard=True)

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

ADMIN_FILE = "admins.json"

def load_admins():
    if os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE) as f:
                return set(int(x) for x in json.load(f))
        except Exception:
            return set()
    return set()

def save_admins(ids):
    with open(ADMIN_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)

admins = load_admins()

def is_admin_user(uid: int) -> bool:
    return uid == OWNER_ID or uid in admins

def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and is_admin_user(u.id))

async def get_client(phone: str, session_str: str) -> TelegramClient:
    if phone in active_clients:
        c = active_clients[phone]
        if not c.is_connected():
            await c.connect()
        return c
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    active_clients[phone] = c
    return c

# ─── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Only owner/admin allowed.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 *Welcome to Account Manager Bot!*\n\nChoose an option 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    return MAIN_MENU

async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    t = update.message.text or ""
    if "➕" in t:
        return await add_start(update, ctx)
    elif "📋" in t:
        return await show_list(update, ctx)
    elif "🟢" in t:
        return await live_start(update, ctx)
    await update.message.reply_text("Please use the buttons 👇", reply_markup=main_kb())
    return MAIN_MENU

async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(load_accounts())
    await update.message.reply_text(
        f"*𝗔𝗟𝗥𝗘𝗔𝗗𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 : {n}*\n\n"
        "নতুন একাউন্ট এর নাম্বার দিন 👇\n\n"
        "_Example : +8801XXXXXXXXX_",
        parse_mode="Markdown"
    )
    return WAIT_PHONE

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone = update.message.text.strip().replace(" ", "")
    ctx.user_data["phone"] = phone
    if phone in load_accounts():
        await update.message.reply_text("⚠️ এই নাম্বার ইতিমধ্যে আছে!", reply_markup=main_kb())
        return MAIN_MENU
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect()
    pending_clients[phone] = c
    try:
        r = await c.send_code_request(phone)
        ctx.user_data["code_hash"] = r.phone_code_hash
        await update.message.reply_text("✅ কোড পাঠানো হয়েছে!\n\n*ভেরিফিকেশন কোড দিন* 👇", parse_mode="Markdown")
        return WAIT_CODE
    except FloodWaitError as e:
        await update.message.reply_text(f"⏳ {e.seconds} সেকেন্ড পরে চেষ্টা করুন।", reply_markup=main_kb())
        await c.disconnect(); del pending_clients[phone]
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_kb())
        await c.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def got_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone  = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session শেষ। আবার /start করুন।", reply_markup=main_kb())
        return MAIN_MENU
    try:
        await client.sign_in(phone, update.message.text.strip(),
                             phone_code_hash=ctx.user_data["code_hash"])
        return await _save_account(update, ctx, client, phone)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 *2FA পাসওয়ার্ড দিন* 👇", parse_mode="Markdown")
        return WAIT_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text("❌ কোড ভুল বা মেয়াদ শেষ।", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone  = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session শেষ।", reply_markup=main_kb())
        return MAIN_MENU
    try:
        await client.sign_in(password=update.message.text.strip())
        return await _save_account(update, ctx, client, phone)
    except Exception as e:
        await update.message.reply_text(f"❌ ভুল পাসওয়ার্ড: {e}", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def _save_account(update, ctx, client, phone):
    me   = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
    accs = load_accounts()
    accs[phone] = {"name": name, "session": client.session.save()}
    save_accounts(accs)
    active_clients[phone] = client
    if phone in pending_clients: del pending_clients[phone]
    await update.message.reply_text(
        f"✅ *Account Login Success!*\n\n👤 `{name}`\n📱 `{phone}`",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return MAIN_MENU

async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accs = load_accounts()
    if not accs:
        await update.message.reply_text("📋 কোনো অ্যাকাউন্ট নেই।", reply_markup=main_kb())
        return MAIN_MENU
    lines = [f"*𝗧𝗼𝘁𝗮𝗹 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗖𝗼𝘂𝗻𝘁 : {len(accs)}*\n"]
    for i, (ph, info) in enumerate(accs.items(), 1):
        lines.append(f"{i}. 👤 `{info['name']}` — `{ph}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())
    return MAIN_MENU

async def live_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not load_accounts():
        await update.message.reply_text("❌ আগে অ্যাকাউন্ট যোগ করুন।", reply_markup=main_kb())
        return MAIN_MENU
    await update.message.reply_text(
        "🎥 *Live link বা @username দিন* 👇\n\n"
        "_Public : @channelname বা t.me/channelname_\n"
        "_Private : t.me/+xxxxxxxx_",
        parse_mode="Markdown"
    )
    return WAIT_LINK

async def got_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    link = update.message.text.strip()
    ctx.user_data["live_link"] = link
    accs = load_accounts()
    total = len(accs)
    if total == 1:
        ctx.user_data["join_count"] = 1
        return await _do_join(update, ctx)
    await update.message.reply_text(
        f"👥 মোট *{total}টি* অ্যাকাউন্ট আছে।\n"
        f"কতটি দিয়ে Join করাবেন? *(1 - {total})*",
        parse_mode="Markdown"
    )
    return WAIT_COUNT

async def got_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    accs = load_accounts()
    try:
        n = int(update.message.text.strip())
        if not 1 <= n <= len(accs): raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ সঠিক সংখ্যা দিন (1 - {len(accs)})")
        return WAIT_COUNT
    ctx.user_data["join_count"] = n
    return await _do_join(update, ctx)

async def _do_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link    = ctx.user_data["live_link"]
    count   = ctx.user_data["join_count"]
    accs    = load_accounts()
    phones  = list(accs.keys())[:count]
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(
        f"⏳ *{count}টি* অ্যাকাউন্ট Join হচ্ছে...", parse_mode="Markdown"
    )
    joined    = []
    calls_map = {}
    errors    = []
    for phone in phones:
        info = accs.get(phone)
        if not info: continue
        try:
            client = await get_client(phone, info["session"])
            entity = await _resolve_and_join(client, link)
            if entity is None:
                errors.append("Link/invite resolve হয়নি (link ভুল অথবা access নেই)")
                continue
            try:
                if isinstance(entity, Channel):
                    full = await client(GetFullChannelRequest(entity))
                else:
                    full = await client(GetFullChatRequest(entity.id))
                call = getattr(full.full_chat, 'call', None)
            except Exception as e:
                try:
                    full = await client(GetFullChatRequest(entity.id))
                    call = getattr(full.full_chat, 'call', None)
                except Exception:
                    call = None
            if call is None:
                errors.append("এখন কোনো চলমান live/voice chat নেই — আগে live চালু করে তারপর join দিন")
                continue
            me     = await client.get_me()
            engine = await _get_engine(phone, client)
            vchat  = get_peer_id(entity)
            await _engine_join(phone, engine, vchat, call, me, client)
            handler = _make_remute_handler(client, call, me.id, phone)
            client.add_event_handler(handler, events.Raw(UpdateGroupCallParticipants))
            live_handlers.setdefault(phone, []).append(handler)
            state = {"engine": engine, "chat_id": vchat, "call": call, "client": client, "me": me}
            live_state[phone] = state
            _start_watchdog(phone, state)
            calls_map[phone] = call
            joined.append(phone)
            logging.info(f"[{phone}] ✅ Joined live via REAL UDP engine")
        except NoActiveGroupCall:
            errors.append(f"{phone}: Live call পাওয়া যায়নি — লাইভ চালু আছে কিনা দেখুন")
        except Exception as e:
            logging.exception(f"[{phone}] Live join failed")
            errors.append(f"{phone}: {type(e).__name__}: {e}")
    if not joined:
        detail = "\n".join(f"• {x}" for x in dict.fromkeys(errors)) or "• অজানা কারণ"
        await msg.edit_text("❌ Join করা সম্ভব হয়নি।\n\n🔎 আসল কারণ:\n" + detail)
        await update.message.reply_text("Main menu 👇", reply_markup=main_kb())
        return MAIN_MENU
    live_sessions[chat_id] = {"phones": joined, "calls": calls_map, "link": link}
    await msg.edit_text(
        f"✅ *YOUR ACCOUNT LIVE JOIN SUCCESS* 🎉\n\n"
        f"✔️ Joined : *{len(joined)}* accounts\n"
        f"❌ Failed : *{count - len(joined)}* accounts\n\n"
        f"📡 Real UDP connection চালু — এখন auto-leave হবে না",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 LIVE LEAVE", callback_data=f"leave:{chat_id}")
        ]])
    )
    if count > len(joined) and errors:
        uniq = list(dict.fromkeys(errors))[:4]
        await update.message.reply_text(
            "⚠️ যেসব account join হয়নি — কারণ:\n" + "\n".join(f"• {x}" for x in uniq)
        )
    return MAIN_MENU

async def _resolve_and_join(client: TelegramClient, link: str):
    try:
        if "t.me/+" in link or "joinchat/" in link:
            invite_hash = (
                link.split("/+")[-1] if "/+" in link
                else link.split("joinchat/")[-1]
            ).strip()
            try:
                check = await client(CheckChatInviteRequest(invite_hash))
                if hasattr(check, 'chat'):
                    return check.chat
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
            except Exception:
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
        else:
            username = (
                link.replace("https://", "").replace("http://", "")
                .replace("t.me/", "").replace("@", "").strip().split("/")[0]
            )
            entity = await client.get_entity(username)
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass
            return entity
    except Exception as e:
        logging.error(f"[RESOLVE] {link} → {e}")
        return None

_last_mute_at: dict = {}

def _make_remute_handler(client: TelegramClient, call, me_id: int, phone: str = None):
    async def handler(ev):
        for p in ev.participants:
            uid = getattr(p.peer, 'user_id', None)
            if uid != me_id:
                continue
            if getattr(p, "left", False):
                logging.warning(f"[KICKED] {phone} live থেকে বের হয়েছে → watchdog ফিরে ঢুকাবে")
                continue
            if not p.muted:
                now = time.monotonic()
                if now - _last_mute_at.get(me_id, 0) < 60:
                    continue
                _last_mute_at[me_id] = now
                try:
                    await client(EditGroupCallParticipantRequest(
                        call=call, participant=InputPeerSelf(), muted=True,
                    ))
                    logging.info(f"[AUTO-MUTE] Re-muted user {me_id}")
                except Exception as ex:
                    logging.warning(f"[AUTO-MUTE] Failed for {me_id}: {ex}")
    return handler

SILENCE_FILE = "silence.wav"

def _ensure_silence() -> str:
    try:
        if not os.path.exists(SILENCE_FILE):
            import wave
            with wave.open(SILENCE_FILE, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000 * 60)
    except Exception as e:
        logging.warning(f"[SILENCE] {e}")
    return SILENCE_FILE

def _silence_stream():
    from pytgcalls.types.raw import Stream, AudioStream, AudioParameters
    from ntgcalls import MediaSource
    return Stream(
        microphone=AudioStream(
            MediaSource.FILE, _ensure_silence(),
            AudioParameters(bitrate=48000, channels=1),
        )
    )

def _tg_filter_stream_end():
    from pytgcalls import filters as tg_filters
    return tg_filters.stream_end()

def _register_stream_loop(engine: PyTgCalls, phone: str):
    @engine.on_update(_tg_filter_stream_end())
    async def _on_stream_end(c, update):
        try:
            await asyncio.sleep(2)
            await c.play(update.chat_id, _silence_stream(),
                         config=GroupCallConfig(auto_start=False))
            logging.info(f"[{phone}] 🔄 silent stream আবার চালু")
        except Exception as e:
            logging.warning(f"[{phone}] stream replay: {e}")

async def _get_engine(phone: str, client: TelegramClient) -> PyTgCalls:
    eng = call_engines.get(phone)
    if eng is None:
        eng = PyTgCalls(client, workers=2)
        call_engines[phone] = eng
        _register_stream_loop(eng, phone)
    if not eng._is_running:
        await eng.start()
    return eng

async def _engine_join(phone: str, engine: PyTgCalls, chat_id: int,
                       call, me, client: TelegramClient):
    try:
        from telethon.tl.types import InputPeerUser
        join_as = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
    except Exception:
        join_as = me
    try:
        await engine.play(chat_id, _silence_stream(),
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    except Exception as e:
        logging.warning(f"[{phone}] silent stream failed ({e}) → media ছাড়া join")
        await engine.play(chat_id, None,
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    try:
        await engine.mute(chat_id)
    except Exception as e:
        logging.warning(f"[{phone}] engine mute: {e}")
    try:
        await client(EditGroupCallParticipantRequest(
            call=call, participant=InputPeerSelf(), muted=True))
    except Exception as e:
        logging.warning(f"[{phone}] mtproto mute: {e}")
    logging.info(f"[{phone}] ✅ ঢুকেছে REAL UDP call-এ (muted)")

async def _conn_state(engine: PyTgCalls, chat_id: int):
    try:
        calls = await engine.calls
        if chat_id not in calls:
            return False, "NOT_IN_CALL"
        return True, "OK"
    except Exception:
        return True, "UNKNOWN"

async def _watchdog_loop(phone: str, state: dict):
    checks = 0
    fails  = 0
    logging.info(f"[WATCHDOG] {phone} 🟢 started (every {KEEPALIVE_INTERVAL}s)")
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            in_call, st_name = await _conn_state(state["engine"], state["chat_id"])
            if in_call:
                checks += 1
                if checks % 15 == 0:
                    logging.info(f"[WATCHDOG] {phone} ✅ live-এ আছে ({checks} checks)")
                continue
            logging.warning(f"[WATCHDOG] {phone} 🔄 connection গেছে → আবার join হচ্ছে")
            if in_call:
                try:
                    await state["engine"].leave_call(state["chat_id"])
                except Exception:
                    pass
                await asyncio.sleep(1)
            await _engine_join(phone, state["engine"], state["chat_id"],
                               state["call"], state["me"], state["client"])
            fails = 0
        except asyncio.CancelledError:
            logging.info(f"[WATCHDOG] {phone} ⛔ stopped")
            raise
        except NoActiveGroupCall:
            logging.warning(f"[WATCHDOG] {phone} ⛔ live শেষ → watchdog বন্ধ")
            break
        except Exception as e:
            fails += 1
            logging.exception(f"[WATCHDOG] {phone} ⚠️ rejoin failed ({fails}x)")
            try:
                await asyncio.sleep(min(KEEPALIVE_INTERVAL * (2 ** fails), 60))
            except asyncio.CancelledError:
                raise

def _start_watchdog(phone: str, state: dict):
    _stop_watchdog(phone)
    watchdog_tasks[phone] = asyncio.create_task(_watchdog_loop(phone, state))

def _stop_watchdog(phone: str):
    task = watchdog_tasks.pop(phone, None)
    if task and not task.done():
        task.cancel()

async def live_leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    chat_id = int(q.data.split(":")[1])
    session = live_sessions.get(chat_id)
    if not session:
        await q.edit_message_text("⚠️ কোনো active session নেই।")
        return
    await q.edit_message_text("⏳ সব অ্যাকাউন্ট Leave হচ্ছে...")
    left = 0
    accs = load_accounts()
    for phone in session["phones"]:
        _stop_watchdog(phone)
        st     = live_state.pop(phone, {})
        engine = st.get("engine")
        vchat  = st.get("chat_id")
        call   = session["calls"].get(phone)
        info   = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(phone, info["session"])
            for h in live_handlers.pop(phone, []):
                try:
                    client.remove_event_handler(h)
                except Exception:
                    pass
            if engine is not None and vchat is not None:
                try:
                    await engine.leave_call(vchat)
                except NotInCallError:
                    pass
            elif call:
                await client(LeaveGroupCallRequest(call=call, source=0))
            left += 1
        except Exception as e:
            logging.error(f"[LEAVE] {phone}: {e}")
    del live_sessions[chat_id]
    await q.edit_message_text(
        f"✅ *{left}টি* অ্যাকাউন্ট Live Leave করেছে।", parse_mode="Markdown"
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ এই কমান্ড শুধুই Owner ব্যবহার করতে পারবেন।")
        return
    if not ctx.args:
        await update.message.reply_text(
            "📖 *ব্যবহার:* `/add user_id`\nউদাহরণ: `/add 123456789`",
            parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id হতে হবে সংখ্যা।")
        return
    if uid == OWNER_ID:
        await update.message.reply_text("⚠️ এটা আপনার নিজের owner ID।")
        return
    if uid in admins:
        await update.message.reply_text(f"⚠️ `{uid}` আগে থেকেই admin।", parse_mode="Markdown")
        return
    admins.add(uid)
    save_admins(admins)
    await update.message.reply_text(f"✅ *Admin যোগ হয়েছে:* `{uid}`", parse_mode="Markdown")

async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ এই কমান্ড শুধুই Owner ব্যবহার করতে পারবেন।")
        return
    if not ctx.args:
        admins_list = "\n".join(f"• `{a}`" for a in sorted(admins)) or "(কোনো admin নেই)"
        await update.message.reply_text(
            "📖 *ব্যবহার:* `/rm user_id`\n\n*বর্তমান admins:*\n" + admins_list,
            parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id হতে হবে সংখ্যা।")
        return
    if uid not in admins:
        await update.message.reply_text(f"⚠️ `{uid}` এখন admin না।", parse_mode="Markdown")
        return
    admins.discard(uid)
    save_admins(admins)
    await update.message.reply_text(f"🗑️ `{uid}` এর admin-ship নেমে গেছে।", parse_mode="Markdown")

async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    owners = f"👑 Owner: `{OWNER_ID}`\n" if OWNER_ID else "👑 Owner: (set হয়নি)\n"
    admins_list = "\n".join(f"• `{a}`" for a in sorted(admins)) or "(কোনো admin নেই)"
    await update.message.reply_text(
        owners + "\n*Admins:*\n" + admins_list, parse_mode="Markdown")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ বাতিল।", reply_markup=main_kb())
    return MAIN_MENU

# ─── HEALTH CHECK SERVER (Render + cron-job.org এর জন্য) ──────────────────────
async def health_handler(request):
    return web.Response(text="✅ Bot is alive!", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"✅ Health server running on port {PORT}")

# ─── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    # Health check server চালু (Render এর জন্য বাধ্যতামূলক)
    await start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU:  [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)],
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            WAIT_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_code)],
            WAIT_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
            WAIT_LINK:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_link)],
            WAIT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_count)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(live_leave_cb, pattern=r"^leave:"))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm",  cmd_rm))
    app.add_handler(CommandHandler("admins", cmd_admins))

    print("🤖 Bot starting...")
    async with app:
        await app.initialize()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(1)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ Bot is running!")
        try:
            await asyncio.Event().wait()
        finally:
            for ph in list(watchdog_tasks):
                _stop_watchdog(ph)
            live_state.clear()

if __name__ == "__main__":
    asyncio.run(main())
