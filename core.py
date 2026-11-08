"""Shared helpers: singletons, personas, keyboards, sending, access control."""
import asyncio
import logging
from collections import OrderedDict
from datetime import datetime

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import LinkPreviewOptions, Message
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

import ai
import config
import fmt
from db import TEHRAN, DB

log = logging.getLogger("roxet.core")

db = DB(config.DB_PATH)
gemini = ai.GeminiService(db)

# ------------------------------------------------------------------ personas
PERSONAS = {
    "default": ("🤖 دستیار عمومی", ""),
    "teacher": (
        "🎓 معلم صبور",
        "نقش تو معلم صبور و خوش‌بیان است: مفاهیم را از ساده به پیچیده، گام‌به‌گام و با مثال‌های روزمره توضیح بده "
        "و در پایان یک سؤال کوتاه برای تمرین بپرس.",
    ),
    "coder": (
        "👨‍💻 برنامه‌نویس ارشد",
        "نقش تو یک مهندس نرم‌افزار ارشد است: کد تمیز و کامل بنویس، ابتدا راه‌حل را کوتاه توضیح بده، "
        "کد را داخل بلوک ``` با نام زبان بگذار و نکات امنیتی و خطاهای رایج را یادآوری کن.",
    ),
    "translator": (
        "🌍 مترجم",
        "نقش تو مترجم حرفه‌ای است. هر متنی که کاربر می‌فرستد را ترجمه کن: اگر فارسی است به انگلیسی، "
        "در غیر این صورت به فارسی روان. فقط ترجمه را بنویس، مگر اینکه کاربر زبان مقصد دیگری بخواهد.",
    ),
    "writer": (
        "✍️ نویسنده و ویراستار",
        "نقش تو نویسنده و ویراستار حرفه‌ای فارسی است: متن‌ها را روان، جذاب و بی‌غلط بنویس یا ویرایش کن "
        "و در صورت ویرایش، تغییرات مهم را در یک فهرست کوتاه بگو.",
    ),
    "analyst": (
        "📊 مشاور و تحلیلگر",
        "نقش تو مشاور کسب‌وکار و تحلیلگر داده است: پاسخ ساختاریافته بده، مزایا/معایب و ریسک‌ها را بگو "
        "و در پایان یک پیشنهاد عملی و قابل‌اجرا بده.",
    ),
    "english": (
        "🇬🇧 معلم انگلیسی",
        "نقش تو معلم مکالمه انگلیسی است: با کاربر به انگلیسی ساده صحبت کن، اشتباهات گرامری او را مؤدبانه "
        "اصلاح کن (با توضیح کوتاه فارسی) و گفتگو را ادامه بده.",
    ),
    "fun": (
        "😄 شوخ‌طبع",
        "نقش تو یک دوست بامزه و شوخ‌طبع است: پاسخ‌ها را دقیق ولی با لحن صمیمی، طنز ملایم و ایموجی بنویس.",
    ),
}

DEFAULT_SYSTEM = (
    f"تو «{config.BOT_NAME}» هستی؛ یک دستیار هوش مصنوعی مفید، دقیق و مودب که با Gemini کار می‌کند. "
    "معمولاً به زبان فارسی پاسخ بده، مگر اینکه کاربر زبان دیگری بخواهد یا به زبان دیگری بنویسد. "
    "اگر جواب را نمی‌دانی صادقانه بگو و اطلاعات ساختگی نده. "
    "پاسخ‌ها را واضح و کاربردی بنویس. از Markdown ساده استفاده کن (**پررنگ**، فهرست، `کد` و بلوک ```کد```) "
    "و از ساختن جدول Markdown خودداری کن."
)


def build_system(persona: str, group: bool, uid: int = None) -> str:
    base = db.get_setting("system_prompt") or DEFAULT_SYSTEM
    parts = [base]
    p = PERSONAS.get(persona or "default", PERSONAS["default"])[1]
    if p:
        parts.append(p)
    # Per-user prompt and response language are private-user preferences.
    if not group and uid:
        row = db.get_user(uid)
        if row:
            if row["personal_prompt"]:
                parts.append("دستور شخصی کاربر (در چارچوب ایمنی و سیاست‌ها): " + row["personal_prompt"][:1200])
            lang = row["response_language"]
            if lang and lang != "auto":
                parts.append("زبان پاسخ را همیشه به این زبان تنظیم کن: " + lang)
    if group:
        parts.append(
            "این گفتگو در یک گروه تلگرام است و هر پیام با نام فرستنده شروع می‌شود؛ "
            "کوتاه و مفید جواب بده و مستقیم به همان شخص پاسخ بده."
        )
    now = datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")
    try:
        import jdatetime
        jnow = jdatetime.datetime.fromgregorian(datetime=datetime.now(TEHRAN)).strftime("%Y/%m/%d %H:%M")
    except Exception:
        jnow = now
    parts.append(f"تاریخ و ساعت فعلی (شمسی/تهران): {jnow}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------- helpers
def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS or db.is_db_admin(uid)


def display_name(user) -> str:
    name = (user.first_name or "").strip() or (user.username or "") or str(user.id)
    return name[:40]


def esc(text) -> str:
    return fmt.strip_html_hint(str(text))


class Typing:
    """Keep the 'typing…' indicator alive while a long request runs."""

    def __init__(self, bot, chat_id, action=ChatAction.TYPING):
        self.bot, self.chat_id, self.action, self.task = bot, chat_id, action, None

    async def _run(self):
        while True:
            try:
                await self.bot.send_chat_action(self.chat_id, self.action)
            except TelegramError:
                pass
            await asyncio.sleep(4)

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        return False


class ImageCache:
    """Keep the last generated images (in RAM) so the 'send as file' button works."""

    def __init__(self, size=20):
        self.size, self.data = size, OrderedDict()

    def put(self, uid, img: bytes):
        self.data[uid] = img
        self.data.move_to_end(uid)
        while len(self.data) > self.size:
            self.data.popitem(last=False)

    def get(self, uid):
        return self.data.get(uid)


images = ImageCache()
chat_locks = {}


def lock_for(chat_id: int) -> asyncio.Lock:
    lock = chat_locks.get(chat_id)
    if lock is None:
        if len(chat_locks) > 5000:
            chat_locks.clear()
        lock = chat_locks[chat_id] = asyncio.Lock()
    return lock


async def reply_ai(message: Message, text: str, markup=None):
    """Send model output as Telegram HTML; fall back to plain text if the markup is rejected."""
    chunks = fmt.split_markdown(text)
    for i, chunk in enumerate(chunks):
        m = markup if i == len(chunks) - 1 else None
        try:
            await message.reply_text(fmt.md_to_html(chunk), parse_mode=ParseMode.HTML, reply_markup=m)
        except BadRequest as exc:
            log.info("HTML rejected (%s); sending plain text", exc)
            try:
                await message.reply_text(chunk, reply_markup=m)
            except TelegramError:
                await message.get_bot().send_message(message.chat_id, chunk, reply_markup=m)


async def safe_edit(query, text, kb=None, html=True):
    try:
        await query.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML if html else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# ------------------------------------------------------------ access control
async def is_member(bot, channel: str, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(channel, uid)
        return m.status not in ("left", "kicked")
    except TelegramError as exc:
        log.warning("Force-join check failed for %s: %s (is the bot an admin of the channel?)", channel, exc)
        return True  # never lock everybody out because of a misconfiguration


def join_keyboard(channel: str):
    return Markup(
        [
            [Btn("📢 عضویت در کانال", url=f"https://t.me/{channel.lstrip('@')}")],
            [Btn("✅ عضو شدم", callback_data="chk")],
        ]
    )


async def gate(update, context, *, quiet_in_groups=False) -> bool:
    """Register the user and enforce ban / maintenance / forced channel membership / group switch."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or user.is_bot:
        return False
    row = db.upsert_user(user.id, user.username, user.first_name)
    in_group = chat.type != ChatType.PRIVATE
    if in_group:
        g = db.upsert_group(chat.id, chat.title or "")
        if not db.get_int("groups_enabled", 1) or not g["enabled"]:
            return False
    admin = is_admin(user.id)
    if row["banned"] and not admin:
        if not quiet_in_groups or not in_group:
            await _say(update, "⛔ دسترسی شما به این ربات مسدود شده است.")
        return False
    if db.get_int("maintenance", 0) and not admin:
        await _say(update, "🛠 ربات در حال به‌روزرسانی است؛ کمی بعد دوباره امتحان کن.")
        return False
    channel = db.get_setting("force_channel")
    if channel and not admin and not await is_member(context.bot, channel, user.id):
        await _say(
            update,
            f"برای استفاده از ربات ابتدا در کانال {esc(channel)} عضو شو و بعد «✅ عضو شدم» را بزن.",
            join_keyboard(channel),
        )
        return False
    return True


async def _say(update, text, kb=None):
    try:
        if update.callback_query:
            await update.callback_query.answer(text[:190], show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def check_quota(update, kind: str) -> bool:
    uid = update.effective_user.id
    if is_admin(uid):
        return True
    row = db.get_user(uid)
    bonus = db.referral_count(uid) * 3
    if kind == "imgs":
        limit = row["custom_daily_imgs"] if row["custom_daily_imgs"] > 0 else db.get_int("daily_imgs", 50)
        if limit > 0: limit += bonus
        used, label = row["imgs_today"], "ساخت/ویرایش تصویر"
    else:
        limit = row["custom_daily_msgs"] if row["custom_daily_msgs"] > 0 else db.get_int("daily_msgs", 0)
        if limit > 0: limit += bonus
        used, label = row["msgs_today"], "پیام"
    if limit > 0 and used >= limit:
        await _say(update, f"⏱ سهمیه‌ی روزانه‌ی {label} ({limit} بار) تمام شده؛ فردا دوباره بیا.")
        return False
    return True


# ------------------------------------------------------------------ keyboards
def main_menu(uid: int, web_on: bool) -> Markup:
    rows = [
        [Btn("💬 گفتگوی جدید", callback_data="menu:new"), Btn("🎨 ساخت تصویر", callback_data="menu:img")],
        [Btn("✏️ ویرایش عکس", callback_data="menu:edit"), Btn("🎭 شخصیت ربات", callback_data="menu:persona")],
        [
            Btn(f"🌐 جستجوی وب: {'روشن ✅' if web_on else 'خاموش ⭕'}", callback_data="menu:web"),
        ],
        [Btn("👤 حساب من", callback_data="menu:me"), Btn("⚙️ تنظیمات من", callback_data="menu:prefs")],
        [Btn("🎁 دعوت دوستان", callback_data="menu:invite"), Btn("❓ راهنما", callback_data="menu:help")],
    ]
    if is_admin(uid):
        rows.append([Btn("🛠 پنل مدیریت", callback_data="adm:home")])
    return Markup(rows)


def persona_menu(current: str) -> Markup:
    rows, row = [], []
    for key, (label, _) in PERSONAS.items():
        row.append(Btn(("✅ " if key == current else "") + label, callback_data=f"persona:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("⬅️ بازگشت", callback_data="menu:main")])
    return Markup(rows)


def back_menu() -> Markup:
    return Markup([[Btn("⬅️ منوی اصلی", callback_data="menu:main")]])


HELP_TEXT = (
    "<b>📖 راهنمای ربات</b>\n\n"
    "💬 <b>گفتگو:</b> هر سؤالی داری همین‌جا بنویس؛ ربات یادش می‌ماند چه گفته‌ای.\n"
    "🖼 <b>عکس:</b> عکس بفرست (با توضیح یا بدون آن) تا تحلیلش کنم؛ متن داخل عکس را هم می‌خوانم.\n"
    "🎙 <b>صدا و ویدیو:</b> ویس یا ویدیو بفرست تا رونویسی/خلاصه کنم.\n"
    "📄 <b>فایل:</b> PDF یا فایل متنی/کد بفرست و درباره‌اش سؤال بپرس.\n"
    "🎨 <b>ساخت تصویر:</b> <code>/img توضیح تصویر</code>\n"
    "✏️ <b>ویرایش عکس:</b> عکس را با کپشن <code>ویرایش: ...</code> بفرست (یا از منو).\n"
    "🌐 <b>جستجوی وب:</b> از منو روشن کن تا با اطلاعات زنده جواب بدهم.\n"
    "🎭 <b>شخصیت:</b> معلم، برنامه‌نویس، مترجم، نویسنده و…\n\n"
    "<b>دستورها</b>\n"
    "/ask سؤال — پرسش (در گروه هم کار می‌کند)\n"
    "/explain متن — توضیح ساده و گام‌به‌گام\n"
    "/summarize — خلاصه (روی یک پیام ریپلای کن یا متن بنویس)\n"
    "/translate — ترجمه\n"
    "/img توضیح — ساخت تصویر\n"
    "/clear — پاک‌کردن حافظه گفتگو\n"
    "/menu — منوی دکمه‌ای\n"
    "/id — نمایش شناسه عددی\n\n"
    "<b>در گروه:</b> ربات را به گروه اضافه کن؛ با <code>/ask</code>، منشن کردن @یوزرنیم ربات، "
    "یا ریپلای روی پیام‌های ربات جواب می‌دهد. روی یک پیام ریپلای کن و "
    "<code>/explain</code> یا <code>/summarize</code> بزن."
)


# ------------------------------------------------------------ admin notices
_last_notice = {}


async def notify_admins(bot, text: str, key: str = None, every: int = 1800):
    """Send a message to all admins; with `key`, at most once per `every` seconds."""
    import time

    if key:
        now = time.time()
        if now - _last_notice.get(key, 0) < every:
            return
        _last_notice[key] = now
    for uid in config.ADMIN_IDS:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except (Forbidden, BadRequest, TelegramError):
            pass
