from telegram import Update, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import re
import json
import io
import asyncio
import random
import string
import httpx
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.ERROR)

# توکن بات را مستقیماً اینجا قرار بده
BOT_TOKEN = "8266974282:AAEQt54_iNNDtn7Epa13uopIbwpGzLPgvxA"
SUPPORT_ID = 7845464086
SUPPORT_USERNAME = "@Aliconfigs"
CHANNEL_ID = "@SelfPersiangulf"
CHANNEL_PRICE = 15000 #مبلغ ماهنه 

user_balances = {}
user_channels = {}

def replace_emoji_ids(text):
    """برای متن‌های داخلی ربات: [CUSTOM_EMOJI_ID] را به تگ HTML تبدیل می‌کند."""
    def repl(match):
        emoji_id = match.group(1)
        # متن داخل تگ فقط fallback است؛ برای پست‌های کاربر تابع async زیر ID را اعتبارسنجی می‌کند.
        return f'<tg-emoji emoji-id="{emoji_id}">▫️</tg-emoji>'
    return re.sub(r'\[(\d{5,})\]', repl, text)

async def process_post_emojis(text, context):
    """Custom Emoji IDهای داخل [ID] را اعتبارسنجی و به HTML تلگرام تبدیل می‌کند."""
    if not isinstance(text, str):
        raise ValueError("متن پیام معتبر نیست")

    emoji_ids = list(dict.fromkeys(re.findall(r'\[(\d{5,})\]', text)))
    if not emoji_ids:
        return text, []

    try:
        stickers = await context.bot.get_custom_emoji_stickers(custom_emoji_ids=emoji_ids)
    except Exception as e:
        logging.error("خطا در get_custom_emoji_stickers", exc_info=True)
        raise RuntimeError(f"بررسی آیدی ایموجی ناموفق بود: {e}")

    emoji_map = {
        str(sticker.custom_emoji_id): (sticker.emoji or "▫️")
        for sticker in stickers
        if getattr(sticker, "custom_emoji_id", None)
    }

    invalid_ids = [eid for eid in emoji_ids if eid not in emoji_map]
    if invalid_ids:
        return None, invalid_ids

    def repl(match):
        eid = match.group(1)
        fallback = emoji_map[eid]
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

    return re.sub(r'\[(\d{5,})\]', repl, text), []

# ═══════════════════ پنل ساخت پک ایموجی (فقط ادمین) ═══════════════════
PACK_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

EMOJI_UNIT_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|(?:[\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF\U0001F000-\U0001FAFF]"
    "[\uFE0F]?[\U0001F3FB-\U0001F3FF]?"
    "(?:\u200D[\u2600-\u27BF\U0001F000-\U0001FAFF][\uFE0F]?[\U0001F3FB-\U0001F3FF]?)*)"
)

_pack_bot_id = None
_pack_bot_username = None

class FloodWait(Exception):
    pass

async def tg_api(method, payload=None, files=None, retries=3):
    """صدا زدن API + مدیریت خودکار محدودیت (429) تلگرام"""
    last_result = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(f"{PACK_API}/{method}", data=payload, files=files)
                data = resp.json()
            if data.get("ok"):
                return data
            if data.get("error_code") == 429:
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                if wait <= 30 and attempt < retries - 1:
                    await asyncio.sleep(wait + 2)
                    continue
                raise FloodWait(
                    f"تلگرام محدودیت زده! {wait} ثانیه ({wait // 60 + 1} دقیقه) صبر کن، "
                    f"بعد دوباره امتحان کن. وسط محدودیت تلاش نکن که بدتر می‌شه."
                )
            last_result = data
        except FloodWait:
            raise
        except Exception as e:
            last_result = {"ok": False, "description": f"{type(e).__name__}: {e}"}
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last_result.get("description", "خطای نامشخص") if last_result else "خطای نامشخص")

def _sanitize(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name.strip()) or "Pack"

def _extract_pack_name(text):
    m = re.search(r"(?:addemoji|addstickers)/([A-Za-z0-9_]+)", text)
    return m.group(1) if m else text.strip()

def _detect_format(s):
    if s.get("is_animated"):
        return "animated"
    if s.get("is_video"):
        return "video"
    return "static"

def _looks_like_ids(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return bool(lines) and all(re.fullmatch(r"\d{5,}(\s*=\s*.+)?", l) for l in lines)

def _norm_emoji(e):
    if isinstance(e, list):
        for item in e:
            if isinstance(item, str):
                m = EMOJI_UNIT_RE.search(item)
                if m:
                    return [m.group(0)]
        return ["😀"]
    if isinstance(e, str):
        m = EMOJI_UNIT_RE.search(e)
        return [m.group(0)] if m else ["😀"]
    return ["😀"]

def _file_meta(it, key):
    if it["format"] == "video":
        return key, ("e.webm", it["data"], "video/webm")
    if it["format"] == "animated":
        return key, ("e.tgs", it["data"], "application/x-tgz")
    ext = "webp" if it["data"][:4] == b"RIFF" else "png"
    return key, (f"e.{ext}", it["data"], f"image/{ext}")

async def _download_sticker(file_id, retries=3):
    last_exc = None
    for attempt in range(retries):
        try:
            res = await tg_api("getFile", {"file_id": file_id})
            if not res.get("ok"):
                raise RuntimeError(f"getFile: {res.get('description')}")
            file_url = f"{FILE_API}/{res['result']['file_path']}"
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.get(file_url)
                r.raise_for_status()
                return r.content
        except Exception as e:
            last_exc = e
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"دانلود فایل ناموفق: {type(last_exc).__name__}: {last_exc}")

async def _fetch_source_items(source_text):
    text = source_text.strip()
    if _looks_like_ids(text):
        ids, emoji_map = [], {}
        for line in text.splitlines():
            line = line.strip()
            if "=" in line:
                eid, emo = line.split("=", 1)
                eid = eid.strip()
                ids.append(eid)
                emoji_map[eid] = _norm_emoji(emo)
            else:
                ids.append(line)
        res = await tg_api("getCustomEmojiStickers", {"custom_emoji_ids": json.dumps(ids)})
        if not res.get("ok"):
            raise RuntimeError(f"getCustomEmojiStickers: {res.get('description')}")
        items = [{
            "data": await _download_sticker(s["file_id"]),
            "emoji": emoji_map.get(s.get("custom_emoji_id"), _norm_emoji(s.get("emoji"))),
            "format": _detect_format(s),
        } for s in res["result"]]
        if not items:
            raise RuntimeError("هیچ ایموجی‌ای پیدا نشد؛ آیدی‌ها رو چک کن")
        return items[:120], "Emoji Pack"
    name = _extract_pack_name(text)
    res = await tg_api("getStickerSet", {"name": name})
    if not res.get("ok"):
        raise RuntimeError(f"پک «{name}» پیدا نشد: {res.get('description')}")
    pack = res["result"]
    items = [{
        "data": await _download_sticker(s["file_id"]),
        "emoji": _norm_emoji(s.get("emoji")),
        "format": _detect_format(s),
    } for s in pack["stickers"]]
    return items[:120], pack.get("title") or name

async def _create_set(name, title, stickers_payload, files):
    res = await tg_api("createNewStickerSet", {
        "user_id": str(_pack_bot_id),
        "name": name,
        "title": (title or "My Emojis")[:64],
        "sticker_type": "custom_emoji",
        "stickers": json.dumps(stickers_payload),
    }, files=files)
    return res

async def _build_pack(short_name, title, items):
    global _pack_bot_id, _pack_bot_username
    if _pack_bot_id is None:
        me = await tg_api("getMe")
        _pack_bot_id = me["result"]["id"]
        _pack_bot_username = me["result"]["username"]

    base = f"{_sanitize(short_name)}_by_{_pack_bot_username}"
    name = base

    stickers, files = [], {}
    for i, it in enumerate(items[:50]):
        k = f"f{i}"
        stickers.append({"sticker": f"attach://{k}", "emoji_list": it["emoji"], "format": it["format"]})
        k, f = _file_meta(it, k)
        files[k] = f

    res = await _create_set(name, title, stickers, files)

    if not res.get("ok") and "occupied" in str(res.get("description", "")):
        suffix = "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        name = base[:64 - len(suffix)] + suffix
        res = await _create_set(name, title, stickers, files)

    if not res.get("ok"):
        raise RuntimeError(f"ساخت پک: {res.get('description')}")

    for idx, it in enumerate(items[50:]):
        k, f = _file_meta(it, "s")
        res = await tg_api("addStickerToSet", {
            "user_id": str(_pack_bot_id),
            "name": name,
            "sticker": json.dumps({"sticker": "attach://s", "emoji_list": it["emoji"], "format": it["format"]}),
        }, files={"s": f})
        if not res.get("ok"):
            raise RuntimeError(f"افزودن ایموجی #{idx + 51}: {res.get('description')}")
        await asyncio.sleep(0.5)  # فاصله بین آپلودها برای جلوگیری از محدودیت
    return name

async def _pack_report(name):
    res = await tg_api("getStickerSet", {"name": name})
    if not res.get("ok"):
        raise RuntimeError("خواندن پک جدید ناموفق بود")
    rows = [("".join(s.get("emoji") or ["😀"]), s["custom_emoji_id"])
            for s in res["result"]["stickers"] if s.get("custom_emoji_id")]
    return res["result"].get("title", name), rows

async def pack_panel_start(update, context):
    context.user_data['waiting_for_pack_name'] = True
    text = ("🎨 <b>ساخت پک ایموجی</b>\n\n"
            "۱. یه <b>اسم انگلیسی</b> برای پک بفرست (حروف/عدد/_، بدون فاصله)\n"
            "مثال: <code>MyEmojis</code>\n\nلغو: /cancel")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text, parse_mode="HTML")

async def pack_panel_cmd(update, context):
    if update.effective_user.id != SUPPORT_ID:
        return
    await pack_panel_start(update, context)

async def cancel_pack_cmd(update, context):
    if update.effective_user.id != SUPPORT_ID:
        return
    had = context.user_data.pop('waiting_for_pack_name', False) or context.user_data.pop('waiting_for_pack_source', False)
    context.user_data.pop('pack_name', None)
    if had:
        await update.message.reply_text("لغو شد ✅", parse_mode="HTML")

async def pack_ids_cmd(update, context):
    """آیدی‌های عددی یه پک موجود رو می‌ده — بدون ساخت پک جدید"""
    if update.effective_user.id != SUPPORT_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "اسم پک رو بده (یا لینکش):\n"
            "<code>/packids MyEmojis</code>\n"
            "<code>/packids t.me/addemoji/MyEmojis_by_Kia4729_bot</code>",
            parse_mode="HTML")
        return
    raw = context.args[0]
    m = re.search(r"(?:addemoji|addstickers)/([A-Za-z0-9_]+)", raw)
    name = m.group(1) if m else raw.strip()
    prog = await update.message.reply_text("⏳ دارم پک رو می‌خونم...")
    try:
        pt, rows = await _pack_report(name)
    except Exception as e:
        await prog.edit_text(f"❌ پک «{name}» پیدا نشد یا خطا داد:\n{e}")
        return
    lines = [f"📦 پک <b>{pt}</b> — {len(rows)} ایموجی:", ""]
    sample = []
    for emo, eid in rows:
        lines.append(f"{emo} → <code>{eid}</code>")
        sample.append(f'<tg-emoji emoji-id="{eid}">{emo}</tg-emoji>')
    text = "\n".join(lines)
    if len(text) > 4000:
        f = io.BytesIO(text.encode())
        f.name = f"{name}_ids.txt"
        await prog.delete()
        await update.message.reply_document(f, caption=f"📁 آیدی‌های پک {pt}")
    else:
        await prog.edit_text(text, parse_mode="HTML")
    for i in range(0, len(sample), 30):
        try:
            await update.message.reply_text("\n".join(sample[i:i + 30]), parse_mode="HTML")
        except:
            pass
# ═══════════════════ پایان پنل پک ═══════════════════

async def check_user_joined(user_id, context):
    try:
        chat_id = CHANNEL_ID
        if CHANNEL_ID.startswith('@'):
            chat = await context.bot.get_chat(CHANNEL_ID)
            chat_id = chat.id
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
        return False
    except:
        return False

def create_join_keyboard():
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "عضویت در کانال", "url": "https://t.me/SelfPersiangulf", "style": "primary"},
                {"text": "بررسی عضویت", "callback_data": "check_join", "style": "success", "icon_custom_emoji_id": "6258234230396949553"}
            ]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_main_keyboard(is_admin=False):
    keyboard = {
        "inline_keyboard": [
            [{"text": "انتخاب کانال", "callback_data": "select_channel", "style": "primary", "icon_custom_emoji_id": "5105062921902229396"}],
            [
                {"text": "افزایش موجودی", "callback_data": "increase_balance", "style": "success", "icon_custom_emoji_id": "5105285714740774968"},
                {"text": "راهنما ربات", "callback_data": "help", "style": "danger", "icon_custom_emoji_id": "5105130477442827969"}
            ],
            [{"text": "پنل نمایندگی", "callback_data": "agency", "style": "primary", "icon_custom_emoji_id": "5102788607869978573"}],
            [
                {"text": "حساب کاربری", "callback_data": "user_account", "style": "success", "icon_custom_emoji_id": "5105267113237415580"},
                {"text": "پشتیبانی", "callback_data": "support", "style": "danger", "icon_custom_emoji_id": "5102958950567905106"}
            ]
        ]
    }
    if is_admin:
        keyboard["inline_keyboard"].append([{"text": "🎨 ساخت پک ایموجی", "callback_data": "packpanel"}])
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_channel_keyboard(user_id):
    channels = user_channels.get(user_id, [])
    if not channels:
        keyboard = {
            "inline_keyboard": [
                [{"text": "ثبت کانال", "callback_data": "register_channel", "style": "primary", "icon_custom_emoji_id": "5789782521084385229"}],
                [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
            ]
        }
        return InlineKeyboardMarkup.de_json(keyboard, None)
    
    inline_keyboard = []
    for channel in channels:
        button_text = f"[5985379274524202415] {channel}"
        button_text = replace_emoji_ids(button_text)
        inline_keyboard.append([{"text": button_text, "callback_data": f"channel_{channel}", "style": "primary"}])
    
    inline_keyboard.append([{"text": "ثبت کانال", "callback_data": "register_channel", "style": "primary", "icon_custom_emoji_id": "5789782521084385229"}])
    inline_keyboard.append([{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}])
    
    keyboard = {"inline_keyboard": inline_keyboard}
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_back_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_agency_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_support_buttons():
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "پیوی پشتیبانی", "url": "https://t.me/Aliconfigs", "style": "primary", "icon_custom_emoji_id": "5102958950567905106"},
                {"text": "پشتیبانی ربات", "callback_data": "support_bot", "style": "primary", "icon_custom_emoji_id": "5102788607869978573"}
            ],
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_support_bot_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "بازگشت", "callback_data": "back_support", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_admin_reply_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "پاسخ به کاربر", "callback_data": "reply_user", "style": "primary", "icon_custom_emoji_id": "5312523882547150460"}],
            [{"text": "بازگشت", "callback_data": "back_support", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_amount_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "num_1"}, {"text": "2", "callback_data": "num_2"}, {"text": "3", "callback_data": "num_3"}],
            [{"text": "4", "callback_data": "num_4"}, {"text": "5", "callback_data": "num_5"}, {"text": "6", "callback_data": "num_6"}],
            [{"text": "7", "callback_data": "num_7"}, {"text": "8", "callback_data": "num_8"}, {"text": "9", "callback_data": "num_9"}],
            [
                {"text": "حذف", "callback_data": "num_delete", "style": "danger", "icon_custom_emoji_id": "5814503317753040871"},
                {"text": "0", "callback_data": "num_0"},
                {"text": "تایید", "callback_data": "num_confirm", "style": "success", "icon_custom_emoji_id": "5814669863699878746"}
            ],
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_admin_verify_keyboard():
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "تایید", "callback_data": "verify_accept", "style": "success", "icon_custom_emoji_id": "6257971296794057056"},
                {"text": "رد کردن", "callback_data": "verify_reject", "style": "danger", "icon_custom_emoji_id": "5830451652309553634"}
            ]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_receipt_sent_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "پیوی ادمین", "url": "https://t.me/aliconfigs", "style": "primary", "icon_custom_emoji_id": "5814670671153730702"}],
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_not_enough_balance_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "افزایش موجودی", "callback_data": "increase_balance", "style": "success", "icon_custom_emoji_id": "5951762148886582569"}],
            [{"text": "بازگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

def create_channel_registered_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "برگشت", "callback_data": "back_main", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    return InlineKeyboardMarkup.de_json(keyboard, None)

async def show_join_message(update, context):
    join_text = (
        f'<b>[6260176972953949338] سلام عشقم</b>\n\n'
        f'<b>[6258234230396949553] خوش اومدی به ربات</b>\n\n'
        f'<b>[6257971296794057056] اول یه جوین توی چنل زیر بده بعد بریم حال کنیم :)</b>\n'
        f'<b>🔗 <a href="https://t.me/SelfPersiangulf">https://t.me/SelfPersiangulf</a></b>'
    )
    final_text = replace_emoji_ids(join_text)
    reply_markup = create_join_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
    else:
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)

async def show_main_panel(update, context):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    first_name = user.first_name if user.first_name else user.username if user.username else "کاربر"
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    welcome_text = (
        f'<blockquote><b>[5417969813609795944] سلام {first_name} خوش اومدی به ربات ایموجی پرمیوم [5210783786706436474]</b></blockquote>\n\n'
        f'<b>با این ربات راحت میتونی ایموجی پرمیوم به کانالت ارسال کنی :) [5104937770850191412]</b>'
    )
    final_text = replace_emoji_ids(welcome_text)
    reply_markup = create_main_keyboard(user.id == SUPPORT_ID)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_joined = await check_user_joined(user.id, context)
    if not is_joined and user.id != SUPPORT_ID:
        await show_join_message(update, context)
        return
    await show_main_panel(update, context)

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    is_joined = await check_user_joined(user.id, context)
    if is_joined:
        success_text = f'<b>[6258234230396949553] عضویت شما تایید شد</b>\n\n<b>[6260176972953949338] خوش اومدی به ربات عزیزم</b>'
        final_text = replace_emoji_ids(success_text)
        await query.edit_message_text(final_text, parse_mode="HTML")
        await show_main_panel(update, context)
    else:
        not_joined_text = f'<b>[6257971296794057056] شما هنوز در کانال عضو نشدید!</b>\n\n<b>لطفاً ابتدا روی دکمه "عضویت در کانال" کلیک کنید و عضو شوید</b>\n<b>سپس دکمه "بررسی عضویت" را بزنید.</b>'
        final_text = replace_emoji_ids(not_joined_text)
        await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_join_keyboard(), disable_web_page_preview=True)

async def show_select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    user_id = user.id
    channels = user_channels.get(user_id, [])
    if not channels:
        channel_text = f'[6258046600455656031] اوخ هیچ کانال که ثبت نکردی [6219810752887262728]\nروی دکمه ثبت کانال بزن کانال ثبت کن :) [6298514001361897127]'
        final_text = replace_emoji_ids(channel_text)
        reply_markup = create_channel_keyboard(user_id)
        if update.callback_query:
            await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        channel_text = f'[5785054788588673518] برای ارسال پست روی اسم کانالی که میخوای پست ارسال بشه کلیک کن :)[5789636015454952676]'
        final_text = replace_emoji_ids(channel_text)
        reply_markup = create_channel_keyboard(user_id)
        if update.callback_query:
            await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=reply_markup)

async def register_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    balance = user_balances.get(user_id, 0)
    if balance < CHANNEL_PRICE:
        not_enough_text = f'[5922608666795580060] ههههههه موجودی نداری [5922608666795580060] [5922608666795580060] [5922608666795580060] [5922608666795580060]\n\nبرو اول هر کانال که میخوای ثبت کنی 30 تومنه برو اول موجودی افزایش بده بعد بیا [5922608666795580060]'
        final_text = replace_emoji_ids(not_enough_text)
        await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_not_enough_balance_keyboard())
        return
    register_text = f'[5922626194557115284] حله داش چنل ثبت کن [5922626194557115284]\n\nبرای ثبت چنلت اول رباتو توی کانالی که میخوای اد فول کن بعد یوزرنیم (ایدی) کانالو بفرست چک کنم [5920529949868964881]'
    final_text = replace_emoji_ids(register_text)
    back_keyboard = {
        "inline_keyboard": [
            [{"text": "بازگشت", "callback_data": "back_select_channel", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    reply_markup = InlineKeyboardMarkup.de_json(back_keyboard, None)
    await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
    context.user_data['waiting_for_channel'] = True

async def handle_channel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    channel_input = update.message.text.strip()
    if not context.user_data.get('waiting_for_channel'):
        return
    if not channel_input.startswith('@'):
        await update.message.reply_text("❌ لطفاً آیدی کانال را با @ وارد کنید.\nمثال: @my_channel", parse_mode="HTML")
        return
    try:
        chat = await context.bot.get_chat(channel_input)
        try:
            test_text = "[5969739636447122230] تست ادمینی ربات"
            test_text_final = replace_emoji_ids(test_text)
            test_message = await context.bot.send_message(chat_id=chat.id, text=test_text_final, parse_mode="HTML")
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=test_message.message_id)
            except:
                pass
        except:
            not_admin_text = f'[5981136177548235329] نشد داش ربات ادمین نی اول اد کن دوباره یوزرنیم بفرست'
            final_text = replace_emoji_ids(not_admin_text)
            await update.message.reply_text(final_text, parse_mode="HTML")
            return
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user_id)
            if member.status not in ['administrator', 'creator']:
                not_admin_text = f'[5981136177548235329] نشد داش ربات ادمین نی اول اد کن دوباره یوزرنیم بفرست'
                final_text = replace_emoji_ids(not_admin_text)
                await update.message.reply_text(final_text, parse_mode="HTML")
                return
        except:
            not_admin_text = f'[5981136177548235329] نشد داش ربات ادمین نی اول اد کن دوباره یوزرنیم بفرست'
            final_text = replace_emoji_ids(not_admin_text)
            await update.message.reply_text(final_text, parse_mode="HTML")
            return
        if user_id in user_channels and channel_input in user_channels[user_id]:
            duplicate_text = f'[5961042782939255288] کانال قبلا ثبت شده داش ریدی [5961042782939255288] مبلغو بک میزنم بت ی کانال دیگه ثبت کن اشحححح[5961042782939255288]'
            final_text = replace_emoji_ids(duplicate_text)
            await update.message.reply_text(final_text, parse_mode="HTML")
            return
        user_balances[user_id] -= CHANNEL_PRICE
        if user_id not in user_channels:
            user_channels[user_id] = []
        user_channels[user_id].append(channel_input)
        success_text = f'[5271801931814165886] کانالت ثبت شد جیگر بزن رو دکمه زیر از اول بیا [5422444280473998663]'
        final_text = replace_emoji_ids(success_text)
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_channel_registered_keyboard())
        context.user_data['waiting_for_channel'] = False
        await context.bot.send_message(
            chat_id=SUPPORT_ID,
            text=f"✅ کانال جدید ثبت شد\n\n👤 کاربر: {user.first_name} (@{user.username if user.username else 'ندارد'})\n🆔 آیدی: {user.id}\n📢 کانال: {channel_input}\n💰 موجودی باقی‌مانده: {user_balances[user_id]:,} تومان",
            parse_mode="HTML"
        )
    except:
        await update.message.reply_text("❌ خطا در ثبت کانال! لطفاً آیدی را به درستی وارد کنید.\nمثال: @my_channel", parse_mode="HTML")

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    help_text = (
        f'<blockquote><b>[5105328363766024275] راهنمای کامل ربات [5105201194079356308]</b></blockquote>\n\n'
        f'<blockquote><b>[5104937770850191412] هزینه اشتراک یکماه برای هر کانال: 30 هزار تومان هست که میتونید برای کانال خود یکماه اشتراک خرید کرده و یا اینکه با دعوت چند نفر دوستاتون میتونید اشتراک بگیرید [5102719042284685213]</b></blockquote>\n\n'
        f'<blockquote><b>راهنما ارسال پست [5309758950105696330]</b></blockquote>\n'
        f'<b>1.</b> ابتدا روی دکمه انتخاب کانال کلیک کرده و سپس کانالی که میخواید پست ارسال کنید رو انتخاب کنید و روی اسم کانال کلیک کنید [5816711927375602899]\n\n'
        f'[5818716826699307883] (اگر کانالی ندارید بر روی دکمه ثبت کانال کلیک کنید اول ربات رو توی کانال خودتون ادمین فول کنید و سپس ایدی کانال خود را ارسال کنید) [5818716826699307883]\n\n'
        f'<b>2.</b> بعد از اینکه کانال خودتونو انتخاب کردین روی کانال کلیک کردین پنل ربات ویرایش شد از کانال ایموجی ها میتونید ایموجی خودتون کد ایموجی رو کپی کنید و به ربات همراه متن ارسال کنید [5816913107938712568]\n\n'
        f'مثلا: \nسلام (این بخش کد ایموجی) \nقرار بدید و به ربات ارسال کنید\n\n'
        f'<blockquote><b>[5819051035284479206] متنی که به کانال شما ارسال میشه به صورت زیر خواهد بود با ایموجی که قرار داده اید.</b></blockquote>\n'
        f'سلام [5818704981179505821]'
    )
    final_text = replace_emoji_ids(help_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_back_keyboard())
        await update.callback_query.answer()
    else:
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_back_keyboard())

async def show_agency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    agency_text = (
        f'<blockquote><b>[5309964275312249008] پنل نمایندگی ربات ایموجی پرمیوم [5309964275312249008]</b></blockquote>\n\n'
        f'<blockquote><b>[5105188334947272972] قابلیت های اصلی:</b></blockquote>\n'
        f'پنل ادمینی کامل تحت اختیار خودتون [5102580731452851920]\n'
        f'مدیریت کاربران + مدیریت کامل حساب [5104964872093828911]\n'
        f'رایگان شدن پنل برای کانال های خودتون [5105308976283649855]\n\n'
        f'کسب درامد + اسپانسری کانالتون  [5105062921902229396]\n\n'
        f'<blockquote><b>[5105288081267754884] هزینه اسپانسری دائمی ربات به قیمت 450T ارائه میشود و به صورت کامل میتوانید ربات خودتون رو داشته باشید. [5105267113237415580]</b></blockquote>'
    )
    final_text = replace_emoji_ids(agency_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_agency_keyboard())
        await update.callback_query.answer()
    else:
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_agency_keyboard())

async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    support_text = f'[5105267113237415580] شما به پشتیبانی وصل شدید [5105267113237415580]\nاز دکمه های زیر میتوانید با پشتیبانی ارتباط بگیرید [5105188334947272972]\nبرای پیگیری سریع مشکل یا سوالتون به پیوی پشتیبانی مراجعه کنید و یا میتوانید از دکمه پشتیبانی ربات ارتباط بگیرید [5105241751455533359]'
    final_text = replace_emoji_ids(support_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_support_buttons())
        await update.callback_query.answer()

async def show_support_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    support_bot_text = (
        f'<blockquote><b>[5102788607869978573] پشتیبانی ربات</b></blockquote>\n'
        f'[5102938893070632240] کاربر گرامی در صورت ارسال هرگونه پیام نامربوط پشتیبانی اجازه مسدود کردن حساب شمارا دارد. [5102938893070632240]\n\n'
        f'<b>[5352647939472760941] پیام خود را ارسال کنید:</b>'
    )
    final_text = replace_emoji_ids(support_bot_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_support_bot_keyboard())
        await update.callback_query.answer()
        context.user_data['waiting_for_support_message'] = True

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_text = update.message.text
    admin_text = (
        f'[6024048153580277867] پیام جدید از سوی کاربر\n'
        f'[6296577138615125756] <a href="tg://user?id={user.id}">ایدی عددی کاربر: {user.id}</a>\n'
        f'[5253478042256308885] یوزرنیم کاربر: @{user.username if user.username else "ندارد"}\n'
        f'[5474667187258006816] موجودی حساب: {user_balances.get(user.id, 0):,} تومان\n'
        f'[5472363852131736400] متن پیام کاربر: {user_text}'
    )
    final_text = replace_emoji_ids(admin_text)
    context.bot_data['reply_user_id'] = user.id
    try:
        await context.bot.send_message(chat_id=SUPPORT_ID, text=final_text, parse_mode="HTML", reply_markup=create_admin_reply_keyboard())
        confirm_text = f'[5105267113237415580] <b>متن شما به پشتیبانی ارسال شد</b> [5105267113237415580]\n\n[5105188334947272972] پیام شما دریافت و به پشتیبانی ارسال گردید\n[5105241751455533359] در اسرع وقت پاسخ داده خواهد شد'
        final_confirm = replace_emoji_ids(confirm_text)
        await update.message.reply_text(final_confirm, parse_mode="HTML")
    except:
        await update.message.reply_text("❌ خطا در ارسال پیام! لطفاً دوباره تلاش کنید.", parse_mode="HTML")
    context.user_data['waiting_for_support_message'] = False

async def admin_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reply_text = f'<b>[5422388729366992632] پیام خود را ارسال کنید:</b>'
    final_text = replace_emoji_ids(reply_text)
    back_keyboard = {
        "inline_keyboard": [
            [{"text": "بازگشت", "callback_data": "back_support", "style": "danger", "icon_custom_emoji_id": "5107127053119915925"}]
        ]
    }
    reply_markup = InlineKeyboardMarkup.de_json(back_keyboard, None)
    await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=reply_markup)
    context.user_data['waiting_for_admin_reply'] = True

async def send_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_text = update.message.text
    user_id = context.bot_data.get('reply_user_id')
    if not user_id:
        await update.message.reply_text("❌ خطا: کاربر یافت نشد!", parse_mode="HTML")
        return
    user_message = f'[5980933747149640196] <b>پیام از سوی ادمین:</b>\n\n{admin_text}'
    final_text = replace_emoji_ids(user_message)
    try:
        await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode="HTML")
        await update.message.reply_text("✅ پیام با موفقیت به کاربر ارسال شد!", parse_mode="HTML")
        context.user_data['waiting_for_admin_reply'] = False
    except:
        await update.message.reply_text("❌ خطا در ارسال پیام!", parse_mode="HTML")

async def show_increase_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user if update.effective_user else update.callback_query.from_user
    if user.id != SUPPORT_ID:
        is_joined = await check_user_joined(user.id, context)
        if not is_joined:
            await show_join_message(update, context)
            return
    balance_text = (
        f'<blockquote><b>[4999002445444023072] افزایش موجودی [4999002445444023072]</b></blockquote>\n'
        f'حداقل مبلغ افزایش موجودی 30,000 تومان هست[5834643712189141114]\n'
        f'[5830223696920318502] از منو زیر میتوانید مبلغ مورد نظر برای افزایش موجودی وارد کنید.\n\n'
        f'<b>[5814670671153730702]مبلغ وارد شده: {context.user_data.get("amount", "")}</b>'
    )
    final_text = replace_emoji_ids(balance_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_amount_keyboard())
        await update.callback_query.answer()
    else:
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_amount_keyboard())

async def show_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = context.user_data.get('amount', '0')
    user = update.effective_user
    first_name = user.first_name if user.first_name else user.username if user.username else "کاربر"
    payment_text = (
        f'<b>[5830326445422940546] مبلغ تعیین شده: {amount} تومان</b>\n'
        f'<b>[5830221171479548287]شماره کارت برای افزایش موجودی:</b>\n<b>6277601405245190</b>\n'
        f'<b>[5830204369567485741]نام: زارعان</b>\n\n'
        f'<blockquote><b>[5830203935775789535]{first_name} ابتدا مبلغ تعیین شده را به شماره کارت بالا انتقال داده و سپس عکس رسید را در همین بخش ارسال کنید.[5834933356193649751]</b></blockquote>\n\n'
        f'<b>[5830348293921576631]رسید شما: </b>'
    )
    final_text = replace_emoji_ids(payment_text)
    if update.callback_query:
        await update.callback_query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_back_keyboard())
        await update.callback_query.answer()
    context.user_data['waiting_for_receipt'] = True

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        amount = context.user_data.get('amount', '0')
        if not amount or amount == '0':
            await update.message.reply_text("❌ خطا: مبلغ وارد نشده است. لطفاً دوباره تلاش کنید.", parse_mode="HTML")
            context.user_data['waiting_for_receipt'] = False
            return
        photo = update.message.photo[-1]
        file_id = photo.file_id
        receipt_info = (
            f'[5830256132513338127] <b>رسید جدید از کاربر</b> [5830256132513338127]\n'
            f'[5834605246462039136] <b>نام کاربر:</b> {user.first_name} [5834605246462039136]\n'
            f'[5830221171479548287] <b>یوزرنیم:</b> @{user.username if user.username else "ندارد"} [5830221171479548287]\n'
            f'[5834422787661369616] <b>آیدی عددی:</b> <code>{user.id}</code> [5834422787661369616]\n'
            f'[5834933356193649751] <b>مبلغ:</b> {amount} تومان [5834933356193649751]\n'
            f'[5830389143355528677] <b>وضعیت:</b> در انتظار تایید [5830389143355528677]'
        )
        final_info = replace_emoji_ids(receipt_info)
        context.bot_data['pending_user_id'] = user.id
        context.bot_data['pending_amount'] = amount
        context.bot_data['pending_username'] = user.username
        context.bot_data['pending_message_id'] = None
        sent_message = await context.bot.send_photo(chat_id=SUPPORT_ID, photo=file_id, caption=final_info, parse_mode="HTML", reply_markup=create_admin_verify_keyboard())
        context.bot_data['pending_message_id'] = sent_message.message_id
        confirm_text = f'<b>[5830256132513338127] رسید شما با موفقیت به ادمین ارسال شد [5830203935775789535]</b>\n\n<b>[5830404222985704156] تا دقایقی دیگر نتیجه به شما اعلام میشود</b>'
        final_text = replace_emoji_ids(confirm_text)
        await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_receipt_sent_keyboard())
        context.user_data['waiting_for_receipt'] = False
    except:
        await update.message.reply_text("❌ خطا در ارسال رسید!", parse_mode="HTML")

async def verify_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user_id = context.bot_data.get('pending_user_id')
        amount = context.bot_data.get('pending_amount', '0')
        message_id = context.bot_data.get('pending_message_id')
        if not user_id:
            await query.answer("❌ خطا: اطلاعات کاربر یافت نشد!", show_alert=True)
            return
        if query.data == "verify_accept":
            if user_id not in user_balances:
                user_balances[user_id] = 0
            user_balances[user_id] += int(amount)
            accept_text = f'[5773677501825945508] <b>رسید شما تایید شد</b> [5773677501825945508]\n\n[6219549292458150316] <b>موجودی فعلی:</b> {user_balances[user_id]:,} تومان\n[6257971296794057056] <b>مبلغ افزوده شده:</b> {amount} تومان\n\n[6257971296794057056] <b>باتشکر از اعتماد شما</b> [6257971296794057056]'
            final_text = replace_emoji_ids(accept_text)
            try:
                await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode="HTML")
                await context.bot.send_message(chat_id=SUPPORT_ID, text=f"[6257971296794057056] رسید با مبلغ {amount} تومان تایید شد", parse_mode="HTML")
                if message_id:
                    try:
                        await context.bot.edit_message_caption(chat_id=SUPPORT_ID, message_id=message_id, caption=f"[6257971296794057056] رسید تایید شد - مبلغ: {amount} تومان", parse_mode="HTML")
                    except:
                        pass
                await query.answer("✅ رسید تایید شد!", show_alert=True)
            except:
                await query.answer(f"❌ خطا!", show_alert=True)
        elif query.data == "verify_reject":
            reject_text = f'[5830451652309553634] <b>عدم تایید رسید</b> [5830451652309553634]\nکاربر گرامی رسید شما توسط ادمین تایید نشد [5832353674281620438]\nبرای اطلاعات بیشتر به پیوی ادمین از طریق بخش پشتیبانی مراجعه کنید [5830348293921576631]'
            final_text = replace_emoji_ids(reject_text)
            try:
                await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode="HTML")
                await context.bot.send_message(chat_id=SUPPORT_ID, text=f"[5830451652309553634] رسید با مبلغ {amount} تومان رد شد", parse_mode="HTML")
                if message_id:
                    try:
                        await context.bot.edit_message_caption(chat_id=SUPPORT_ID, message_id=message_id, caption=f"[5830451652309553634] رسید رد شد - مبلغ: {amount} تومان", parse_mode="HTML")
                    except:
                        pass
                await query.answer("❌ رسید رد شد!", show_alert=True)
            except:
                await query.answer(f"❌ خطا!", show_alert=True)
        context.bot_data.pop('pending_user_id', None)
        context.bot_data.pop('pending_amount', None)
        context.bot_data.pop('pending_username', None)
        context.bot_data.pop('pending_message_id', None)
    except:
        pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        if query.data == "check_join":
            await check_join(update, context)
        elif query.data == "select_channel":
            await show_select_channel(update, context)
        elif query.data == "register_channel":
            await register_channel_start(update, context)
        elif query.data == "back_select_channel":
            await show_select_channel(update, context)
        elif query.data.startswith("channel_"):
            channel = query.data.replace("channel_", "")
            selected_text = f'[5210783786706436474] کانال مورد نظر انتخاب شد[5213222108359849454]\n@{channel} [5213157559296358731]\n\nبرای ارسال پست از کانال @CustomEmojiPack ایدی ایموجیو کپی کنید همراه با متن ارسال کنید. [5321529789016740670]'
            final_text = replace_emoji_ids(selected_text)
            await query.edit_message_text(final_text, parse_mode="HTML")
            context.user_data['selected_channel'] = channel
            context.user_data['waiting_for_post'] = True
        elif query.data == "increase_balance":
            context.user_data['amount'] = ''
            await show_increase_balance(update, context)
        elif query.data.startswith("num_"):
            action = query.data.replace("num_", "")
            current_amount = context.user_data.get('amount', '')
            if action == "delete":
                current_amount = current_amount[:-1]
            elif action == "confirm":
                if not current_amount:
                    await query.answer("⚠️ لطفاً مبلغ را وارد کنید!")
                    return
                try:
                    amount_int = int(current_amount)
                    if amount_int < 30000:
                        await query.answer("⚠️ حداقل مبلغ 30,000 تومان است!", show_alert=True)
                        return
                    else:
                        await show_payment_info(update, context)
                        return
                except ValueError:
                    await query.answer("⚠️ لطفاً عدد معتبر وارد کنید!", show_alert=True)
                    return
            else:
                current_amount += action
            context.user_data['amount'] = current_amount
            await show_increase_balance(update, context)
        elif query.data == "help":
            await show_help(update, context)
        elif query.data == "agency":
            await show_agency(update, context)
        elif query.data == "user_account":
            user = query.from_user
            if user.id != SUPPORT_ID:
                is_joined = await check_user_joined(user.id, context)
                if not is_joined:
                    await show_join_message(update, context)
                    return
            balance = user_balances.get(user.id, 0)
            account_text = f'<blockquote><b>[5105267113237415580] حساب کاربری [5105267113237415580]</b></blockquote>\n\n<b>[5104964872093828911] آیدی عددی:</b> <code>{user.id}</code>\n<b>[5102580731452851920] نام:</b> {user.first_name}\n<b>[5105308976283649855] موجودی:</b> {balance:,} تومان\n<b>[5105062921902229396] وضعیت:</b> فعال'
            final_text = replace_emoji_ids(account_text)
            await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_back_keyboard())
        elif query.data == "support":
            await show_support(update, context)
        elif query.data == "support_bot":
            await show_support_bot(update, context)
        elif query.data == "reply_user":
            await admin_reply_to_user(update, context)
        elif query.data == "back_support":
            await show_support(update, context)
        elif query.data == "packpanel":
            if query.from_user.id == SUPPORT_ID:
                await pack_panel_start(update, context)
        elif query.data == "back_main":
            user = query.from_user
            first_name = user.first_name if user.first_name else user.username if user.username else "کاربر"
            if user.id != SUPPORT_ID:
                is_joined = await check_user_joined(user.id, context)
                if not is_joined:
                    await show_join_message(update, context)
                    return
            context.user_data.clear()
            welcome_text = f'<blockquote><b>[5417969813609795944] سلام {first_name} خوش اومدی به ربات ایموجی پرمیوم [5210783786706436474]</b></blockquote>\n\n<b>با این ربات راحت میتونی ایموجی پرمیوم به کانالت ارسال کنی :) [5104937770850191412]</b>'
            final_text = replace_emoji_ids(welcome_text)
            await query.edit_message_text(final_text, parse_mode="HTML", reply_markup=create_main_keyboard(user.id == SUPPORT_ID))
        elif query.data in ["verify_accept", "verify_reject"]:
            await verify_receipt(update, context)
    except:
        pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if user.id != SUPPORT_ID:
            is_joined = await check_user_joined(user.id, context)
            if not is_joined:
                join_text = f'<b>[6257971296794057056] شما در کانال عضو نیستید!</b>\n\n<b>لطفاً ابتدا در کانال زیر عضو شوید:</b>\n<b>🔗 <a href="https://t.me/SelfPersiangulf">https://t.me/SelfPersiangulf</a></b>'
                final_text = replace_emoji_ids(join_text)
                await update.message.reply_text(final_text, parse_mode="HTML", reply_markup=create_join_keyboard(), disable_web_page_preview=True)
                return
        # ─── فلوی ساخت پک ایموجی (فقط ادمین) ───
        if user.id == SUPPORT_ID and context.user_data.get('waiting_for_pack_name'):
            name = update.message.text.strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{2,40}", name):
                await update.message.reply_text("❌ اسم نامعتبره! فقط حروف انگلیسی، عدد و _ — دوباره بفرست:")
                return
            context.user_data['pack_name'] = name
            context.user_data['waiting_for_pack_name'] = False
            context.user_data['waiting_for_pack_source'] = True
            await update.message.reply_text(
                "۲. حالا <b>سورس</b> رو بفرست؛ یکی از دو حالت:\n\n"
                "🔗 <b>کپی کل پک:</b> لینکش رو بفرست:\n<code>t.me/addemoji/PackName</code>\n\n"
                "🔢 <b>آیدی دستی:</b> هر خط یه آیدی (ایموجی اختیاری):\n"
                "<code>5368324170671202286 = 😎</code>",
                parse_mode="HTML")
            return
        if user.id == SUPPORT_ID and context.user_data.get('waiting_for_pack_source'):
            source = update.message.text.strip()
            if not (re.search(r"addemoji|addstickers", source) or _looks_like_ids(source)):
                await update.message.reply_text("❌ فرمت درست نیست!\nیا لینک t.me/addemoji/... بفرست یا لیست آیدی‌ها. (لغو: /cancel)")
                return
            context.user_data['waiting_for_pack_source'] = False
            short = context.user_data.pop('pack_name', 'Pack')
            prog = await update.message.reply_text("⏳ در حال دانلود فایل‌ها از مبدا...")
            try:
                items, title = await _fetch_source_items(source)
                await prog.edit_text(f"⏳ {len(items)} ایموجی گرفتم؛ دارم پک رو با هویت خود بات می‌سازم...")
                full = await _build_pack(short, title, items)
                pt, rows = await _pack_report(full)
            except Exception as e:
                logging.error("خطای ساخت پک:", exc_info=True)
                err_text = str(e).strip() if str(e).strip() else type(e).__name__
                try:
                    await prog.edit_text(f"❌ خطا: {err_text}")
                except:
                    await update.message.reply_text(f"❌ خطا: {err_text}")
                return
            lines = [f"✅ پک <b>{pt}</b> ساخته شد!", f"نام پک: <code>{full}</code>", ""]
            sample = []
            for emo, eid in rows:
                lines.append(f"{emo} → <code>{eid}</code>")
                sample.append(f'<tg-emoji emoji-id="{eid}">{emo}</tg-emoji>')
            text = "\n".join(lines)
            if len(text) > 4000:
                f = io.BytesIO(text.encode())
                f.name = f"{short}_ids.txt"
                await update.message.reply_document(f, caption=f"📁 آیدی‌های عددی پک {pt}")
            else:
                await update.message.reply_text(text, parse_mode="HTML")
            for i in range(0, len(sample), 30):
                try:
                    await context.bot.send_message(chat_id=SUPPORT_ID,
                        text="\n".join(sample[i:i + 30]), parse_mode="HTML")
                except:
                    pass
            return
        # ─── پایان فلوی پک ───
        if context.user_data.get('waiting_for_channel'):
            await handle_channel_registration(update, context)
            return
        if context.user_data.get('waiting_for_post'):
            channel = context.user_data.get('selected_channel')
            user_text = update.message.text or ""

            if not channel:
                context.user_data['waiting_for_post'] = False
                await update.message.reply_text("❌ کانال انتخاب‌شده پیدا نشد. دوباره کانال را انتخاب کنید.")
                return

            if not user_text.strip():
                await update.message.reply_text("❌ لطفاً متن پست را ارسال کنید.")
                return

            try:
                final_text, invalid_ids = await process_post_emojis(user_text, context)

                if invalid_ids:
                    ids_text = "\n".join(f"• <code>{eid}</code>" for eid in invalid_ids)
                    await update.message.reply_text(
                        "❌ این آیدی‌های ایموجی معتبر نیستند یا بات به آن‌ها دسترسی ندارد:\n\n"
                        f"{ids_text}\n\n"
                        "فرمت صحیح: <code>سلام [1234567890123456789]</code>",
                        parse_mode="HTML"
                    )
                    return

                await context.bot.send_message(
                    chat_id=channel,
                    text=final_text,
                    parse_mode="HTML"
                )

                confirm_post = (
                    f'[5105062921902229396] '
                    f'<b>پیام شما با موفقیت به کانال {channel} ارسال شد!</b>'
                )
                await update.message.reply_text(
                    replace_emoji_ids(confirm_post),
                    parse_mode="HTML"
                )

                context.user_data['waiting_for_post'] = False
                context.user_data['selected_channel'] = None
                await show_select_channel(update, context)

            except Exception as e:
                logging.error("خطا در ارسال پست", exc_info=True)
                await update.message.reply_text(
                    "❌ خطا در ارسال پیام به کانال.\n\n"
                    f"جزئیات: <code>{str(e)[:500]}</code>",
                    parse_mode="HTML"
                )
            return
        if context.user_data.get('waiting_for_receipt'):
            if update.message.photo:
                await handle_receipt(update, context)
                return
            else:
                await update.message.reply_text("⚠️ لطفاً عکس رسید خود را ارسال کنید.", parse_mode="HTML")
                return
        if context.user_data.get('waiting_for_support_message'):
            if user.id != SUPPORT_ID:
                await forward_to_admin(update, context)
                return
            else:
                context.user_data['waiting_for_support_message'] = False
                await update.message.reply_text("شما به عنوان ادمین در حالت پشتیبانی هستید.", parse_mode="HTML")
                return
        if context.user_data.get('waiting_for_admin_reply'):
            if user.id == SUPPORT_ID:
                await send_admin_reply(update, context)
                return
        if user.id == SUPPORT_ID:
            pattern = r'\[(\d+)\]'
            matches = re.findall(pattern, update.message.text)
            if matches:
                new_text = update.message.text
                for emoji_id in matches:
                    replacement = f'<tg-emoji emoji-id="{emoji_id}">😎</tg-emoji>'
                    new_text = new_text.replace(f'[{emoji_id}]', replacement)
                await update.message.reply_text(new_text, parse_mode="HTML")
                return
        if update.message.text and update.message.text.startswith('/help'):
            await show_help(update, context)
            return
        elif update.message.text and update.message.text.startswith('/agency'):
            await show_agency(update, context)
            return
        elif update.message.text and update.message.text.startswith('/support'):
            await show_support(update, context)
            return
        if update.message.text:
            await update.message.reply_text(f"📩 پیام شما دریافت شد:\n\n{update.message.text}", parse_mode="HTML")
    except:
        pass

async def error_handler(update, context):
    logging.error("خطا در پردازش آپدیت:", exc_info=context.error)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("help", show_help, filters=private))
    app.add_handler(CommandHandler("agency", show_agency, filters=private))
    app.add_handler(CommandHandler("support", show_support, filters=private))
    app.add_handler(CommandHandler("packpanel", pack_panel_cmd, filters=private))
    app.add_handler(CommandHandler("packids", pack_ids_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_pack_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & private, handle_message))
    app.add_error_handler(error_handler)
    print("start")
    app.run_polling()

if __name__ == "__main__":
    main()
