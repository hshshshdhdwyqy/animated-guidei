"""In-bot admin panel (inline glass buttons)."""
import asyncio
import logging
import os
import re
from datetime import datetime

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import ai
import config
import core
from core import db, esc, gemini, is_admin, safe_edit
from db import today_jalali

log = logging.getLogger("roxet.admin")
BACK = [Btn("⬅️ پنل مدیریت", callback_data="adm:home")]
CANCEL = Markup([[Btn("❌ لغو", callback_data="adm:home")]])


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if key and len(key) > 10 else ("•••" if key else "—")


def _onoff(v) -> str:
    return "روشن ✅" if v else "خاموش ⭕"


# ------------------------------------------------------------------- screens
def home_kb() -> Markup:
    return Markup(
        [
            [Btn("📊 آمار", callback_data="adm:stats"), Btn("🔑 کلید API", callback_data="adm:key")],
            [Btn("🧠 مدل‌ها", callback_data="adm:models"), Btn("⚙️ تنظیمات", callback_data="adm:set")],
            [Btn("👥 کاربران", callback_data="adm:users"), Btn("💬 گروه‌ها", callback_data="adm:groups")],
            [Btn("📢 پیام همگانی", callback_data="adm:bc"), Btn("💾 بکاپ دیتابیس", callback_data="adm:backup")],
            [Btn("🛡 مدیریت مدیران", callback_data="adm:admins")],
            [Btn("🏠 منوی کاربر", callback_data="menu:main")],
        ]
    )


def home_text() -> str:
    key = "✅ ثبت شده" if gemini.has_key() else "❌ ثبت نشده"
    return (
        f"🛠 <b>پنل مدیریت {esc(config.BOT_NAME)}</b>\n\n"
        f"🔑 کلید Gemini: {key}\n"
        f"🧠 مدل متن: <code>{esc(gemini.text_model)}</code>\n"
        f"🎨 مدل تصویر: <code>{esc(gemini.image_model)}</code>"
    )


def stats_text() -> str:
    s = db.stats()
    return (
        "📊 <b>آمار ربات</b>\n\n"
        f"👤 کاربران: <b>{s['users']}</b>  (امروز فعال: {s['active_today']}، جدید امروز: {s['new_today']})\n"
        f"⛔ مسدود: {s['banned']}   🚫 ربات را بلاک کرده‌اند: {s['blocked']}\n"
        f"💬 گروه‌ها: {s['groups']}\n\n"
        f"💬 پیام امروز: {s['msgs_today']}   🎨 تصویر امروز: {s['imgs_today']}\n"
        f"📈 مجموع کل: {s['total_msgs']} پیام، {s['total_imgs']} تصویر\n"
        f"📅 تاریخ شمسی: {today_jalali()}"
    )


def key_screen():
    key = gemini.api_key
    src = "پنل مدیریت" if (db.get_setting("gemini_key") or db.get_setting("gemini_keys")) else ("فایل .env" if key else "—")
    text = (
        "🔑 <b>کلید API Gemini</b>\n\n"
        f"وضعیت: {'✅ ثبت شده' if key else '❌ ثبت نشده'}\n"
        f"کلید: <code>{esc(_mask(key))}</code>  (منبع: {src})\n\n"
        "کلید را از <b>aistudio.google.com/apikey</b> بساز. توجه: اشتراک Gemini Pro/Google AI Pro "
        "خودش کلید API نمی‌دهد؛ کلید از Google AI Studio ساخته می‌شود."
    )
    kb = Markup(
        [
            [Btn("✏️ ثبت / تغییر کلید", callback_data="adm:key_set")],
            [Btn("🧪 تست اتصال", callback_data="adm:key_test"), Btn("🗑 حذف کلید", callback_data="adm:key_del")],
            BACK,
        ]
    )
    return text, kb


def models_screen():
    text = (
        "🧠 <b>مدل‌ها</b>\n\n"
        f"📝 مدل متن: <code>{esc(gemini.text_model)}</code>\n"
        f"🎨 مدل تصویر: <code>{esc(gemini.image_model)}</code>\n\n"
        "«انتخاب از لیست» مدل‌های واقعیِ در دسترس کلید شما را از خود Google می‌گیرد."
    )
    kb = Markup(
        [
            [Btn("📋 لیست مدل‌های متن", callback_data="adm:ml:t"), Btn("✏️ تایپ مدل متن", callback_data="adm:mt:t")],
            [Btn("📋 لیست مدل‌های تصویر", callback_data="adm:ml:i"), Btn("✏️ تایپ مدل تصویر", callback_data="adm:mt:i")],
            [Btn("🧪 تست ساخت تصویر", callback_data="adm:imgtest")],
            BACK,
        ]
    )
    return text, kb


def settings_screen():
    dm, di = db.get_int("daily_msgs", 50), db.get_int("daily_imgs", 5)
    ch = db.get_setting("force_channel")
    text = (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"💬 سقف پیام روزانه هر کاربر: <b>{dm if dm > 0 else 'نامحدود'}</b>\n"
        f"🎨 سقف تصویر روزانه هر کاربر: <b>{di if di > 0 else 'نامحدود'}</b>\n"
        f"📢 عضویت اجباری: <b>{esc(ch) if ch else 'خاموش'}</b>\n"
        f"💬 کار در گروه‌ها: {_onoff(db.get_int('groups_enabled', 1))}\n"
        f"🛠 حالت تعمیر: {_onoff(db.get_int('maintenance', 0))}\n"
        f"👋 متن خوش‌آمد سفارشی: {'دارد' if db.get_setting('welcome') else 'پیش‌فرض'}\n"
        f"🧬 پرامپت سیستم سفارشی: {'دارد' if db.get_setting('system_prompt') else 'پیش‌فرض'}\n\n"
        "مدیران از سقف روزانه معاف‌اند."
    )
    kb = Markup(
        [
            [Btn("💬 سقف پیام", callback_data="adm:s:dmsgs"), Btn("🎨 سقف تصویر", callback_data="adm:s:dimgs")],
            [Btn("📢 کانال اجباری", callback_data="adm:s:channel")],
            [
                Btn(f"💬 گروه‌ها: {_onoff(db.get_int('groups_enabled', 1))}", callback_data="adm:tog:groups_enabled"),
                Btn(f"🛠 تعمیر: {_onoff(db.get_int('maintenance', 0))}", callback_data="adm:tog:maintenance"),
            ],
            [Btn("👋 متن خوش‌آمد", callback_data="adm:s:welcome"), Btn("🧬 پرامپت سیستم", callback_data="adm:s:prompt")],
            BACK,
        ]
    )
    return text, kb


def user_card(row):
    name = esc(row["first_name"] or "—")
    uname = f"@{esc(row['username'])}" if row["username"] else "—"
    text = (
        f"👤 <b>{name}</b>  {uname}\n🆔 <code>{row['id']}</code>\n"
        f"وضعیت: {'⛔ مسدود' if row['banned'] else '✅ عادی'}"
        f"{'  🚫 بلاک‌کرده' if row['blocked'] else ''}\n"
        f"امروز: {row['msgs_today']} پیام، {row['imgs_today']} تصویر\n"
        f"مجموع: {row['total_msgs']} پیام، {row['total_imgs']} تصویر\n"
        f"شخصیت: {esc(core.PERSONAS.get(row['persona'], core.PERSONAS['default'])[0])}"
    )
    kb = Markup(
        [
            [
                Btn("✅ رفع مسدودی" if row["banned"] else "⛔ مسدود کن", callback_data=f"adm:ub:{row['id']}"),
                Btn("♻️ ریست سهمیه امروز", callback_data=f"adm:ur:{row['id']}"),
            ],
            [Btn("✉️ ارسال پیام", callback_data=f"adm:um:{row['id']}")],
            [Btn("⬅️ کاربران", callback_data="adm:users")],
        ]
    )
    return text, kb


def users_list_kb(rows) -> Markup:
    buttons = []
    for r in rows:
        label = f"{'⛔ ' if r['banned'] else ''}{(r['first_name'] or '—')[:18]} · {r['id']}"
        buttons.append([Btn(label, callback_data=f"adm:u:{r['id']}")])
    buttons.append(BACK)
    return Markup(buttons)


async def _screen(update, text, kb=None):
    q = update.callback_query
    if q:
        await safe_edit(q, text, kb)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ----------------------------------------------------------------- callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    act = parts[1] if len(parts) > 1 else "home"
    arg = parts[2:] if len(parts) > 2 else []
    chat_id = update.effective_chat.id
    await q.answer()

    if act == "home":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("bc", None)
        await safe_edit(q, home_text(), home_kb())
    elif act == "stats":
        await safe_edit(q, stats_text(), Markup([[Btn("🔄 تازه‌سازی", callback_data="adm:stats")], BACK]))

    # --------------------------------------------------------------- admins
    elif act == "admins":
        ids = sorted(set(config.ADMIN_IDS) | set(db.list_admins()))
        lines = [f"• <code>{uid}</code>" + (" — مدیر اصلی" if uid in config.ADMIN_IDS else " — مدیر قابل‌حذف") for uid in ids]
        kb_rows = [[Btn("➕ افزودن مدیر", callback_data="adm:adadd")]]
        for uid in ids:
            if uid not in config.ADMIN_IDS:
                kb_rows.append([Btn(f"🗑 حذف مدیر {uid}", callback_data=f"adm:adrm:{uid}")])
        kb_rows.append(BACK)
        await safe_edit(q, "🛡 <b>مدیریت مدیران</b>\n\n" + ("\n".join(lines) or "هنوز مدیری اضافه نشده است."), Markup(kb_rows))
    elif act == "adadd":
        if update.effective_user.id not in config.ADMIN_IDS:
            await safe_edit(q, "⛔ فقط مدیر اصلی می‌تواند مدیر جدید اضافه کند.", Markup([BACK]))
            return
        ctx.user_data["await"] = ("a_addadmin",)
        await safe_edit(q, "آیدی عددی کاربر را بفرست. کاربر باید حداقل یک‌بار ربات را Start کرده باشد.", CANCEL)
    elif act == "adrm":
        if update.effective_user.id not in config.ADMIN_IDS:
            await safe_edit(q, "⛔ فقط مدیر اصلی می‌تواند مدیر حذف کند.", Markup([BACK]))
            return
        uid = int(arg[0])
        db.remove_admin(uid)
        await safe_edit(q, f"✅ دسترسی مدیریتی <code>{uid}</code> حذف شد.", Markup([[Btn("🛡 مدیران", callback_data="adm:admins")], BACK]))

    # ------------------------------------------------------------- API key
    elif act == "key":
        await safe_edit(q, *key_screen())
    elif act == "key_set":
        ctx.user_data["await"] = ("a_key",)
        await safe_edit(
            q,
            "🔑 <b>کلید API</b> را همین‌جا بفرست.\n\n"
            "• از https://aistudio.google.com/apikey بساز (شروع معمولاً با <code>AIza</code>).\n"
            "• پیامی که کلید را در آن می‌فرستی بلافاصله پاک می‌شود.\n"
            "• ربات اول کلید را تست می‌کند و فقط اگر معتبر بود ذخیره می‌کند.",
            CANCEL,
        )
    elif act == "key_del":
        db.del_setting("gemini_key")
        await safe_edit(q, *key_screen())
    elif act == "key_test":
        await safe_edit(q, "🧪 در حال تست اتصال به Gemini…", None)
        try:
            ans = await gemini.chat([], [ai.text_part("فقط یک جمله‌ی کوتاه فارسی سلام بگو.")], "Be brief.")
            text = f"✅ اتصال برقرار است.\nمدل: <code>{esc(gemini.text_model)}</code>\nپاسخ: {esc(ans[:200])}"
        except ai.AIError as err:
            text = f"❌ {esc(err.user_msg)}\n\n<code>{esc(err.detail[:500])}</code>"
        await safe_edit(q, text, Markup([[Btn("🧠 مدل‌ها", callback_data="adm:models")], BACK]))

    # -------------------------------------------------------------- models
    elif act == "models":
        await safe_edit(q, *models_screen())
    elif act == "ml":  # list models: adm:ml:t|i
        kind = arg[0]
        await safe_edit(q, "📋 در حال دریافت لیست مدل‌ها از Google…", None)
        try:
            models = await gemini.list_models()
        except ai.AIError as err:
            await safe_edit(q, f"❌ {esc(err.user_msg)}", Markup([[Btn("🔑 کلید API", callback_data="adm:key")], BACK]))
            return
        except Exception as exc:  # noqa: BLE001
            e = ai.classify_error(exc)
            await safe_edit(q, f"❌ {esc(e.user_msg)}\n<code>{esc(e.detail[:300])}</code>", Markup([BACK]))
            return
        names = gemini.filter_text_models(models) if kind == "t" else gemini.filter_image_models(models)
        names = names[:14]
        ctx.bot_data[f"mlist:{kind}"] = names
        if not names:
            await safe_edit(q, "مدلی پیدا نشد. می‌توانی نام مدل را دستی تایپ کنی.", Markup([[Btn("⬅️ مدل‌ها", callback_data="adm:models")]]))
            return
        cur = gemini.text_model if kind == "t" else gemini.image_model
        rows = [
            [Btn(("✅ " if n == cur else "") + n, callback_data=f"adm:pk:{kind}:{i}")] for i, n in enumerate(names)
        ]
        rows.append([Btn("⬅️ مدل‌ها", callback_data="adm:models")])
        title = "📝 مدل متن" if kind == "t" else "🎨 مدل تصویر"
        await safe_edit(q, f"{title} را انتخاب کن:", Markup(rows))
    elif act == "pk":  # pick: adm:pk:t:3
        kind, idx = arg[0], int(arg[1])
        names = ctx.bot_data.get(f"mlist:{kind}", [])
        if idx < len(names):
            db.set_setting("text_model" if kind == "t" else "image_model", names[idx])
        await safe_edit(q, *models_screen())
    elif act == "mt":  # typed model
        ctx.user_data["await"] = ("a_tmodel" if arg[0] == "t" else "a_imodel",)
        hint = "gemini-3.8-flash" if arg[0] == "t" else "gemini-2.5-flash-image"
        await safe_edit(q, f"نام دقیق مدل را بفرست. مثال: <code>{hint}</code>", CANCEL)
    elif act == "imgtest":
        await safe_edit(q, "🧪 در حال تست ساخت تصویر…", None)
        try:
            img, _ = await gemini.generate_image("a small red apple on a white table, studio photo")
            await ctx.bot.send_photo(chat_id, img, caption=f"✅ مدل تصویر کار می‌کند: {gemini.image_model}")
            text = "✅ ساخت تصویر موفق بود."
        except ai.AIError as err:
            text = f"❌ {esc(err.user_msg)}\n\n<code>{esc(err.detail[:500])}</code>"
        await safe_edit(q, text, Markup([[Btn("🧠 مدل‌ها", callback_data="adm:models")], BACK]))

    # ------------------------------------------------------------ settings
    elif act == "set":
        await safe_edit(q, *settings_screen())
    elif act == "tog":
        key = arg[0]
        if key in ("groups_enabled", "maintenance"):
            default = 1 if key == "groups_enabled" else 0
            db.set_setting(key, 0 if db.get_int(key, default) else 1)
        await safe_edit(q, *settings_screen())
    elif act == "s":
        what = arg[0]
        prompts = {
            "dmsgs": "عدد سقف پیام روزانه‌ی هر کاربر را بفرست (<code>0</code> = نامحدود):",
            "dimgs": "عدد سقف تصویر روزانه‌ی هر کاربر را بفرست (<code>0</code> = نامحدود):",
            "channel": "یوزرنیم کانال را بفرست؛ مثل <code>@mychannel</code>.\nربات باید ادمین کانال باشد. برای خاموش‌کردن <code>0</code> بفرست:",
            "welcome": "متن خوش‌آمدگویی را بفرست. از <code>{name}</code> برای نام کاربر استفاده کن. برای بازگشت به پیش‌فرض <code>0</code> بفرست:",
            "prompt": "پرامپت سیستم (دستور اصلی رفتار ربات) را بفرست. برای بازگشت به پیش‌فرض <code>0</code> بفرست:",
        }
        ctx.user_data["await"] = ("a_" + what,)
        await safe_edit(q, prompts[what], CANCEL)

    # --------------------------------------------------------------- users
    elif act == "users":
        kb = Markup(
            [
                [Btn("🔎 جستجو (آیدی/یوزرنیم)", callback_data="adm:ufind")],
                [Btn("🕒 جدیدترین‌ها", callback_data="adm:ul:r"), Btn("🏆 فعال‌ترین‌ها", callback_data="adm:ul:t")],
                BACK,
            ]
        )
        await safe_edit(q, "👥 <b>مدیریت کاربران</b>", kb)
    elif act == "ul":
        rows = db.top_users(10) if arg[0] == "t" else db.recent_users(10)
        await safe_edit(q, "👥 یک کاربر را انتخاب کن:", users_list_kb(rows))
    elif act == "ufind":
        ctx.user_data["await"] = ("a_find",)
        await safe_edit(q, "آیدی عددی یا یوزرنیم کاربر را بفرست:", CANCEL)
    elif act in ("u", "ub", "ur", "um"):
        uid = int(arg[0])
        row = db.get_user(uid)
        if not row:
            await safe_edit(q, "کاربر پیدا نشد.", Markup([BACK]))
            return
        if act == "ub":
            db.set_user_field(uid, "banned", 0 if row["banned"] else 1)
        elif act == "ur":
            db.reset_today(uid)
        elif act == "um":
            ctx.user_data["await"] = ("a_umsg", uid)
            await safe_edit(q, f"متنی که می‌خواهی برای کاربر <code>{uid}</code> ارسال شود را بفرست:", CANCEL)
            return
        await safe_edit(q, *user_card(db.get_user(uid)))

    # -------------------------------------------------------------- groups
    elif act == "groups":
        rows = db.list_groups(12)
        if not rows:
            await safe_edit(q, "هنوز در هیچ گروهی نیستم. ربات را به یک گروه اضافه کن.", Markup([BACK]))
            return
        buttons = [
            [Btn(f"{'✅' if g['enabled'] else '⭕'} {(g['title'] or '—')[:28]}", callback_data=f"adm:g:{g['id']}")]
            for g in rows
        ]
        buttons.append(BACK)
        await safe_edit(q, "💬 <b>گروه‌ها</b> — یکی را برای مدیریت انتخاب کن:", Markup(buttons))
    elif act in ("g", "gt", "gl"):
        gid = int(arg[0])
        g = db.get_group(gid)
        if not g:
            await safe_edit(q, "گروه پیدا نشد.", Markup([BACK]))
            return
        if act == "gt":
            db.set_group_enabled(gid, not g["enabled"])
        elif act == "gl":
            try:
                await ctx.bot.leave_chat(gid)
            except TelegramError:
                pass
            db.deactivate_group(gid)
            await safe_edit(q, "🚪 از گروه خارج شدم.", Markup([[Btn("⬅️ گروه‌ها", callback_data="adm:groups")]]))
            return
        g = db.get_group(gid)
        kb = Markup(
            [
                [Btn("⭕ غیرفعال کن" if g["enabled"] else "✅ فعال کن", callback_data=f"adm:gt:{gid}")],
                [Btn("🚪 خروج از گروه", callback_data=f"adm:gl:{gid}")],
                [Btn("⬅️ گروه‌ها", callback_data="adm:groups")],
            ]
        )
        await safe_edit(
            q, f"💬 <b>{esc(g['title'])}</b>\n🆔 <code>{gid}</code>\nوضعیت: {_onoff(g['enabled'])}", kb
        )

    # ----------------------------------------------------------- broadcast
    elif act == "bc":
        ctx.user_data["await"] = ("a_bc",)
        await safe_edit(
            q,
            "📢 <b>پیام همگانی</b>\nپیامی که می‌خواهی برای همه‌ی کاربران ارسال شود را بفرست "
            "(متن، عکس، ویدیو… هر چیزی). اول پیش‌نمایش می‌بینی و بعد تأیید می‌کنی.",
            CANCEL,
        )
    elif act == "bcno":
        ctx.user_data.pop("bc", None)
        await safe_edit(q, "لغو شد.", Markup([BACK]))
    elif act == "bcgo":
        src = ctx.user_data.pop("bc", None)
        if not src:
            await safe_edit(q, "پیامی برای ارسال نیست.", Markup([BACK]))
            return
        targets = db.broadcast_targets()
        await safe_edit(q, f"📤 ارسال برای {len(targets)} کاربر شروع شد؛ پایانش گزارش می‌دهم.", Markup([BACK]))
        task = asyncio.create_task(_broadcast(ctx.bot, chat_id, src[0], src[1], targets))
        ctx.bot_data.setdefault("tasks", set()).add(task)
        task.add_done_callback(ctx.bot_data["tasks"].discard)

    # -------------------------------------------------------------- backup
    elif act == "backup":
        path = config.DATA_DIR / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        try:
            await asyncio.to_thread(db.backup_to, path)
            with open(path, "rb") as fh:
                await ctx.bot.send_document(chat_id, fh, filename=path.name, caption="💾 بکاپ دیتابیس")
        finally:
            if path.exists():
                os.remove(path)
    elif act == "close":
        try:
            await q.message.delete()
        except TelegramError:
            pass


async def _broadcast(bot, admin_chat, src_chat, src_msg, targets):
    ok = fail = 0
    for uid in targets:
        for attempt in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
                break
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except Forbidden:
                db.set_user_field(uid, "blocked", 1)
                fail += 1
                break
            except TelegramError:
                fail += 1
                break
        await asyncio.sleep(0.06)
    try:
        await bot.send_message(admin_chat, f"📢 ارسال همگانی تمام شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}")
    except TelegramError:
        pass


# ---------------------------------------------------------- typed admin input
async def consume_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle the admin's next message when the panel is waiting for input. Returns True if consumed."""
    state = ctx.user_data.get("await")
    if not state or not str(state[0]).startswith("a_"):
        return False
    msg = update.effective_message
    chat_id = update.effective_chat.id
    name = state[0]
    text = (msg.text or "").strip()
    ctx.user_data.pop("await", None)
    back = Markup([BACK])

    async def say(t, kb=back):
        await ctx.bot.send_message(chat_id, t, parse_mode=ParseMode.HTML, reply_markup=kb)

    if name == "a_bc":
        ctx.user_data["bc"] = (chat_id, msg.message_id)
        n = len(db.broadcast_targets())
        await ctx.bot.copy_message(chat_id, chat_id, msg.message_id)
        await say(
            f"👆 پیش‌نمایش پیام. ارسال برای <b>{n}</b> کاربر؟",
            Markup([[Btn(f"✅ ارسال برای {n} کاربر", callback_data="adm:bcgo"), Btn("❌ لغو", callback_data="adm:bcno")]]),
        )
        return True

    if not text:
        ctx.user_data["await"] = state
        await say("فقط متن بفرست.", CANCEL)
        return True

    if name == "a_addadmin":
        if update.effective_user.id not in config.ADMIN_IDS:
            await say("⛔ فقط مدیر اصلی اجازه‌ی افزودن مدیر دارد.")
            return True
        if not text.isdigit() or not db.get_user(int(text)):
            ctx.user_data["await"] = state
            await say("آیدی معتبرِ کاربری که ربات را Start کرده بفرست.", CANCEL)
            return True
        uid = int(text)
        if uid in config.ADMIN_IDS:
            await say("این کاربر از قبل مدیر اصلی است.", Markup([[Btn("🛡 مدیران", callback_data="adm:admins")], BACK]))
            return True
        db.add_admin(uid)
        await say(f"✅ کاربر <code>{uid}</code> مدیر شد.", Markup([[Btn("🛡 مدیران", callback_data="adm:admins")], BACK]))
        return True

    if name == "a_key":
        try:
            await msg.delete()  # never leave the key in the chat
        except TelegramError:
            pass
        key = text.split()[0]
        wait = await ctx.bot.send_message(chat_id, "🧪 در حال بررسی کلید…")
        try:
            models = await gemini.list_models(key)
        except Exception as exc:  # noqa: BLE001
            err = ai.classify_error(exc)
            await wait.edit_text(
                f"❌ کلید پذیرفته نشد.\n{err.user_msg}",
                reply_markup=Markup([[Btn("🔁 دوباره", callback_data="adm:key_set")], BACK]),
            )
            return True
        db.set_setting("gemini_key", key)
        notes = ["✅ کلید ذخیره شد."]
        texts, imgs = gemini.filter_text_models(models), gemini.filter_image_models(models)
        if texts and gemini.text_model not in texts:
            old = gemini.text_model
            flash = [n for n in texts if "flash" in n and "lite" not in n]
            new = (flash or texts)[0]
            db.set_setting("text_model", new)
            notes.append(f"مدل متن «{old}» برای این کلید در دسترس نبود؛ روی «{new}» گذاشته شد.")
        if imgs and gemini.image_model not in imgs:
            new = imgs[0]
            db.set_setting("image_model", new)
            notes.append(f"مدل تصویر روی «{new}» گذاشته شد.")
        notes.append("برای اطمینان «🧪 تست اتصال» را بزن.")
        await wait.edit_text(
            "\n".join(notes),
            reply_markup=Markup([[Btn("🧪 تست اتصال", callback_data="adm:key_test")], BACK]),
        )
        return True

    if name in ("a_tmodel", "a_imodel"):
        model = text.replace("models/", "").strip()
        if not re.fullmatch(r"[\w.\-]{3,80}", model):
            await say("نام مدل معتبر نیست.", CANCEL)
            ctx.user_data["await"] = state
            return True
        db.set_setting("text_model" if name == "a_tmodel" else "image_model", model)
        await say(f"✅ مدل روی <code>{esc(model)}</code> تنظیم شد.", Markup([[Btn("🧠 مدل‌ها", callback_data="adm:models")], BACK]))
        return True

    if name in ("a_dmsgs", "a_dimgs"):
        if not text.isdigit():
            ctx.user_data["await"] = state
            await say("فقط عدد بفرست (0 = نامحدود).", CANCEL)
            return True
        db.set_setting("daily_msgs" if name == "a_dmsgs" else "daily_imgs", int(text))
        await say("✅ ذخیره شد.", Markup([[Btn("⚙️ تنظیمات", callback_data="adm:set")], BACK]))
        return True

    if name == "a_channel":
        if text == "0":
            db.del_setting("force_channel")
            await say("✅ عضویت اجباری خاموش شد.", Markup([[Btn("⚙️ تنظیمات", callback_data="adm:set")], BACK]))
            return True
        ch = text if text.startswith("@") else "@" + text.lstrip("@")
        try:
            member = await ctx.bot.get_chat_member(ch, ctx.bot.id)
            if member.status not in ("administrator", "creator"):
                raise TelegramError("not admin")
        except TelegramError:
            await say(
                f"❌ نتوانستم کانال {esc(ch)} را بررسی کنم. ربات را <b>ادمین</b> آن کانال کن و دوباره امتحان کن.",
                Markup([[Btn("🔁 دوباره", callback_data="adm:s:channel")], BACK]),
            )
            return True
        db.set_setting("force_channel", ch)
        await say(f"✅ عضویت اجباری در {esc(ch)} فعال شد.", Markup([[Btn("⚙️ تنظیمات", callback_data="adm:set")], BACK]))
        return True

    if name in ("a_welcome", "a_prompt"):
        key = "welcome" if name == "a_welcome" else "system_prompt"
        if text == "0":
            db.del_setting(key)
            await say("✅ به حالت پیش‌فرض برگشت.", Markup([[Btn("⚙️ تنظیمات", callback_data="adm:set")], BACK]))
        else:
            db.set_setting(key, text)
            await say("✅ ذخیره شد.", Markup([[Btn("⚙️ تنظیمات", callback_data="adm:set")], BACK]))
        return True

    if name == "a_find":
        row = db.find_user(text)
        if not row:
            await say("کاربری با این مشخصات پیدا نشد.", Markup([[Btn("🔎 دوباره", callback_data="adm:ufind")], BACK]))
        else:
            t, kb = user_card(row)
            await say(t, kb)
        return True

    if name == "a_umsg":
        uid = state[1]
        try:
            await ctx.bot.send_message(uid, f"📩 <b>پیام از مدیریت:</b>\n\n{esc(text)}", parse_mode=ParseMode.HTML)
            await say("✅ ارسال شد.", Markup([[Btn("⬅️ کاربر", callback_data=f"adm:u:{uid}")], BACK]))
        except TelegramError as exc:
            await say(f"❌ ارسال نشد: {esc(str(exc))}")
        return True

    return False


async def cmd_addkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ADMIN_IDS:
        await update.effective_message.reply_text("فقط مدیر اصلی می‌تواند کلید API اضافه کند.")
        return
    args = (update.effective_message.text or "").split(maxsplit=1)
    if len(args) != 2 or len(args[1].strip()) < 15:
        await update.effective_message.reply_text("روش استفاده: /addkey کلید_API")
        return
    keys = gemini.api_keys
    if args[1].strip() not in keys:
        keys.append(args[1].strip())
    db.set_setting("gemini_keys", "\\n".join(keys))
    db.set_setting("gemini_key_index", 0)
    gemini._client = None
    gemini._key = None
    await update.effective_message.reply_text(f"✅ کلید ذخیره شد. تعداد کلیدها: {len(keys)}")


async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    keys = gemini.api_keys
    if not keys:
        await update.effective_message.reply_text("هیچ کلیدی ثبت نشده است.")
        return
    lines = [f"{i+1}. {k[:4]}…{k[-4:]}" for i, k in enumerate(keys)]
    await update.effective_message.reply_text("🔑 کلیدهای ثبت‌شده (چرخش خودکار هنگام محدودیت):\\n" + "\\n".join(lines))


async def cmd_delkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ADMIN_IDS:
        await update.effective_message.reply_text("فقط مدیر اصلی می‌تواند کلید API حذف کند.")
        return
    args = (update.effective_message.text or "").split()
    keys = gemini.api_keys
    if len(args) != 2 or not args[1].isdigit() or not 1 <= int(args[1]) <= len(keys):
        await update.effective_message.reply_text("روش استفاده: /delkey شماره_کلید (از /keys شماره‌ها را ببین)")
        return
    keys.pop(int(args[1])-1)
    db.set_setting("gemini_keys", "\\n".join(keys))
    db.set_setting("gemini_key_index", 0)
    gemini._client = None
    gemini._key = None
    await update.effective_message.reply_text(f"✅ کلید حذف شد. کلیدهای باقی‌مانده: {len(keys)}")


async def cmd_setquota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = (update.effective_message.text or "").split()
    if len(args) != 4 or not all(x.lstrip("-").isdigit() for x in args[1:]):
        await update.effective_message.reply_text("روش استفاده: /setquota شناسه_کاربر تعداد_پیام تعداد_تصویر\\nعدد ۰ یعنی استفاده از سقف عمومی.")
        return
    uid, msgs, imgs = map(int, args[1:])
    if not db.get_user(uid):
        await update.effective_message.reply_text("این کاربر هنوز ربات را شروع نکرده است.")
        return
    db.set_personal_setting(uid, "custom_daily_msgs", max(0, msgs))
    db.set_personal_setting(uid, "custom_daily_imgs", max(0, imgs))
    await update.effective_message.reply_text(f"✅ سهمیه اختصاصی کاربر {uid} تنظیم شد.")


async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ADMIN_IDS:
        await update.effective_message.reply_text("فقط مدیر اصلیِ داخل فایل .env می‌تواند مدیر جدید اضافه کند.")
        return
    args = (update.effective_message.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await update.effective_message.reply_text("روش استفاده: /addadmin شناسه_عددی")
        return
    db.add_admin(int(args[1]))
    await update.effective_message.reply_text("✅ مدیر جدید اضافه شد.")


async def cmd_deladmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ADMIN_IDS:
        await update.effective_message.reply_text("فقط مدیر اصلیِ داخل فایل .env می‌تواند مدیر را حذف کند.")
        return
    args = (update.effective_message.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await update.effective_message.reply_text("روش استفاده: /deladmin شناسه_عددی")
        return
    db.remove_admin(int(args[1]))
    await update.effective_message.reply_text("✅ مدیر حذف شد.")


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("پنل مدیریت فقط در چت خصوصی با ربات باز می‌شود.")
        return
    db.upsert_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    ctx.user_data.pop("await", None)
    await update.effective_message.reply_text(home_text(), parse_mode=ParseMode.HTML, reply_markup=home_kb())
