"""User-facing handlers: chat, media, images, commands, menus, group behaviour."""
import logging
import re
import time

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import admin
import ai
import config
import core
from core import db, esc, gemini, is_admin, reply_ai
from db import today_jalali

log = logging.getLogger("roxet.user")

KIND_LABEL = {
    "photo": "تصویر", "voice": "پیام صوتی", "audio": "فایل صوتی",
    "video": "ویدیو", "pdf": "سند PDF", "text": "فایل متنی",
}
DEFAULT_PROMPTS = {
    "photo": "این تصویر را با دقت بررسی کن و به فارسی توضیح بده. اگر متنی در آن هست آن را هم بخوان و ترجمه کن.",
    "voice": "این پیام صوتی را ابتدا عیناً رونویسی کن (متن گفته‌شده) و سپس به محتوای آن پاسخ بده.",
    "audio": "این فایل صوتی را رونویسی و خلاصه کن.",
    "video": "این ویدیو را بررسی کن و محتوای آن را خلاصه و توضیح بده.",
    "pdf": "این سند را خلاصه کن و نکات اصلی آن را بگو.",
    "text": "این فایل را بررسی و خلاصه کن؛ اگر کد است توضیح بده و اشکالات احتمالی را بگو.",
}
TEXT_EXT = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".java",
    ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".php", ".rb", ".sh", ".sql", ".yaml", ".yml", ".ini",
    ".log", ".toml", ".kt", ".swift", ".lua",
}


# --------------------------------------------------------------------- media
class MediaError(Exception):
    pass


async def extract_media(msg):
    """Return (parts, kind, file_name) for the supported attachment of `msg`, or (None, None, None)."""
    file_id = size = None
    mime, kind, name = "", None, ""
    if msg.photo:
        ph = msg.photo[-1]
        file_id, size, mime, kind = ph.file_id, ph.file_size, "image/jpeg", "photo"
    elif msg.voice:
        file_id, size, mime, kind = msg.voice.file_id, msg.voice.file_size, "audio/ogg", "voice"
    elif msg.audio:
        a = msg.audio
        mime = (a.mime_type or "audio/mpeg").lower()
        if mime in ("audio/x-m4a", "audio/m4a", "audio/mp4"):
            mime = "audio/aac"
        file_id, size, kind = a.file_id, a.file_size, "audio"
    elif msg.video:
        file_id, size, mime, kind = msg.video.file_id, msg.video.file_size, msg.video.mime_type or "video/mp4", "video"
    elif msg.video_note:
        file_id, size, mime, kind = msg.video_note.file_id, msg.video_note.file_size, "video/mp4", "video"
    elif msg.sticker and not (msg.sticker.is_animated or msg.sticker.is_video):
        file_id, size, mime, kind = msg.sticker.file_id, msg.sticker.file_size, "image/webp", "photo"
    elif msg.document:
        d = msg.document
        file_id, size, mime, name = d.file_id, d.file_size, (d.mime_type or "").lower(), d.file_name or "file"
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if mime.startswith("image/"):
            kind = "photo"
        elif mime == "application/pdf":
            kind = "pdf"
        elif mime.startswith("text/") or ext in TEXT_EXT or mime in ("application/json", "application/xml"):
            kind = "text"
        else:
            raise MediaError("این نوع فایل پشتیبانی نمی‌شود. PDF، تصویر، صوت، ویدیو و فایل‌های متنی/کد را می‌توانم بخوانم.")
    else:
        return None, None, None

    if size and size > config.MAX_MEDIA_BYTES:
        raise MediaError("حجم فایل زیاد است (حداکثر ۱۵ مگابایت).")
    try:
        f = await msg.get_bot().get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except BadRequest as exc:
        raise MediaError("دریافت فایل ممکن نشد (احتمالاً حجمش زیاد است).") from exc
    if kind == "text":
        txt = data.decode("utf-8", errors="replace")[: config.MAX_TEXT_FILE_CHARS]
        return [ai.text_part(f"[محتوای فایل «{name}»]\n{txt}\n[پایان فایل]")], kind, name
    return [ai.bytes_part(data, mime)], kind, name


# ------------------------------------------------------------------- helpers
def is_addressed(msg, bot) -> bool:
    r = msg.reply_to_message
    if r and r.from_user and r.from_user.id == bot.id:
        return True
    text = (msg.text or msg.caption or "").lower()
    return bool(bot.username) and f"@{bot.username}".lower() in text


def strip_mention(text: str, bot) -> str:
    if bot.username:
        text = re.sub(re.escape(f"@{bot.username}"), "", text, flags=re.I)
    return text.strip()


def cmd_args(msg) -> str:
    parts = (msg.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def send_error(update, ctx, err: ai.AIError):
    uid = update.effective_user.id
    text = esc(err.user_msg)
    markup = None
    if is_admin(uid):
        if err.detail:
            text += f"\n\n<b>جزئیات (فقط برای مدیر):</b>\n<code>{esc(err.detail[:500])}</code>"
        if err.kind in ("nokey", "key"):
            markup = Markup([[Btn("🔑 ثبت کلید API", callback_data="adm:key")]])
        elif err.kind == "model":
            markup = Markup([[Btn("🧠 انتخاب مدل", callback_data="adm:models")]])
    elif err.kind in ("nokey", "key", "model", "perm", "quota"):
        await core.notify_admins(
            ctx.bot, f"⚠️ <b>هشدار:</b> {esc(err.user_msg)}\n<code>{esc(err.detail[:300])}</code>", key=f"err:{err.kind}"
        )
    try:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError:
        pass


def welcome_text(user) -> str:
    tpl = db.get_setting("welcome")
    if tpl:
        return esc(tpl).replace("{name}", esc(core.display_name(user)))
    return (
        f"سلام {esc(core.display_name(user))} 👋\n"
        f"من «<b>{esc(config.BOT_NAME)}</b>» هستم و با Gemini کار می‌کنم.\n\n"
        "💬 گفتگو و پاسخ به سؤال‌ها\n🖼 تحلیل عکس، ویس، ویدیو و فایل\n🎨 ساخت و ویرایش تصویر\n"
        "🌍 ترجمه و خلاصه‌سازی\n🌐 جستجوی زنده در وب\n\n"
        "یک سؤال بنویس یا از دکمه‌های زیر استفاده کن 👇"
    )


async def show_main(update, ctx, edit=False):
    user = update.effective_user
    row = db.get_user(user.id)
    kb = core.main_menu(user.id, bool(row["web"]))
    text = welcome_text(user)
    if is_admin(user.id) and not gemini.has_key():
        text += "\n\n⚠️ <b>مدیر:</b> هنوز کلید API Gemini ثبت نشده. از «🛠 پنل مدیریت» ثبتش کن."
    if edit and update.callback_query:
        await core.safe_edit(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ------------------------------------------------------------------ AI flows
async def run_chat(update, ctx, text, media=None, kind=None, *, save=True, quoted=None):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await core.check_quota(update, "msgs"):
        return
    group = chat.type != ChatType.PRIVATE
    row = db.get_user(user.id)
    system = core.build_system("default" if group else row["persona"], group, user.id)
    body = text
    if quoted:
        body = f"[پیام ارجاع‌شده]\\n{quoted[:3000]}\\n[پایان پیام ارجاع‌شده]\\n\\n{body}"
    if group:
        body = f"{core.display_name(user)}: {body}"
    parts = [ai.text_part(body)] + list(media or [])
    answer_parts = []
    live = None
    last_live_edit = 0.0
    async with core.lock_for(chat.id):
        history = db.get_history(chat.id, config.HISTORY_TURNS) if save else []
        async with core.Typing(ctx.bot, chat.id):
            try:
                live = await msg.reply_text("✍️ در حال نوشتن پاسخ…")
                async for piece in gemini.stream_chat(history, parts, system, web=bool(row["web"])):
                    answer_parts.append(piece)
                    current = "".join(answer_parts)
                    # Telegram limits message edits; update the same message at a
                    # gentle cadence so the user sees the answer grow while streaming.
                    now = time.monotonic()
                    if current and (now - last_live_edit >= 0.9):
                        try:
                            await live.edit_text(current[-3900:] if len(current) > 3900 else current)
                            last_live_edit = now
                        except TelegramError:
                            # The final formatted edit below is still attempted.
                            pass
                answer = "".join(answer_parts).strip()
                if not answer:
                    # Some model/API combinations do not support streaming.
                    answer = await gemini.chat(history, parts, system, web=bool(row["web"]))
            except ai.AIError as err:
                if live:
                    await _drop(live)
                await send_error(update, ctx, err)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("streaming unavailable; falling back to regular generation: %s", exc)
                try:
                    answer = await gemini.chat(history, parts, system, web=bool(row["web"]))
                except Exception as fallback_exc:
                    if live:
                        await _drop(live)
                    await send_error(update, ctx, ai.classify_error(fallback_exc))
                    return
    if save:
        tag = f"[{KIND_LABEL.get(kind, 'پیوست')}] " if kind else ""
        who = f"{core.display_name(user)}: " if group else ""
        db.add_history(chat.id, "user", f"{who}{tag}{text}"[:4000])
        db.add_history(chat.id, "model", answer[:6000])
    db.incr(user.id, "msgs")
    actions = Markup([[
        Btn("↪️ ادامه بده", callback_data="answer:continue"),
        Btn("✂️ کوتاه‌تر کن", callback_data="answer:short"),
        Btn("🌐 ترجمه", callback_data="answer:translate"),
    ]]) if chat.type == ChatType.PRIVATE else None
    if live:
        try:
            if len(answer) > 3900:
                await _drop(live)
                await reply_ai(msg, answer, actions)
            else:
                await live.edit_text(fmt.md_to_html(answer), parse_mode=ParseMode.HTML, reply_markup=actions)
        except (BadRequest, TelegramError):
            await _drop(live)
            await reply_ai(msg, answer, actions)
    else:
        await reply_ai(msg, answer, actions)
    if chat.type == ChatType.PRIVATE:
        ctx.user_data["last_answer"] = answer[:6000]
        ctx.user_data["last_question"] = text[:3000]


async def do_image(update, ctx, prompt: str, sources=None):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await core.check_quota(update, "imgs"):
        return
    editing = bool(sources)
    note = await ctx.bot.send_message(
        chat.id, "✏️ در حال ویرایش تصویر…" if editing else "🎨 در حال ساخت تصویر… (چند ثانیه صبر کن)"
    )
    try:
        async with core.Typing(ctx.bot, chat.id, ChatAction.UPLOAD_PHOTO):
            img, extra = await gemini.generate_image(prompt, sources)
    except ai.AIError as err:
        await _drop(note)
        await send_error(update, ctx, err)
        return
    except Exception:  # noqa: BLE001
        log.exception("image failed")
        await _drop(note)
        await send_error(update, ctx, ai.classify_error(RuntimeError("unexpected")))
        return
    await _drop(note)
    db.incr(user.id, "imgs")
    private = chat.type == ChatType.PRIVATE
    markup = None
    if private:
        core.images.put(user.id, img)
        ctx.user_data["last_img_prompt"] = prompt
        markup = Markup(
            [
                [Btn("🔄 دوباره بساز", callback_data="img:again"), Btn("📎 دریافت فایل", callback_data="img:file")],
                [Btn("✏️ ویرایش این تصویر", callback_data="img:edit"), Btn("🏠 منو", callback_data="menu:main")],
            ]
        )
    caption = ("🎨 " + prompt)[:300]
    try:
        await msg.reply_photo(img, caption=caption, reply_markup=markup)
    except BadRequest:
        await msg.reply_document(img, filename="roxet-ai.png", caption=caption, reply_markup=markup)
    if extra and len(extra) < 800:
        await reply_ai(msg, extra)


async def _drop(message):
    try:
        await message.delete()
    except TelegramError:
        pass


# ------------------------------------------------------------------ commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    args = cmd_args(update.effective_message)
    if args.startswith("ref_") and args[4:].isdigit():
        inviter = int(args[4:])
        if db.add_referral(inviter, update.effective_user.id):
            try:
                await ctx.bot.send_message(inviter, "🎉 یک نفر با لینک دعوت تو وارد شد! ۳ درخواست رایگان روزانه به سهمیه‌ات اضافه شد.")
            except TelegramError:
                pass
    ctx.user_data.pop("await", None)
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"سلام! من {config.BOT_NAME} هستم 🤖\nبا /ask سؤالت را بپرس، یا منشنم کن، یا روی پیام‌هایم ریپلای کن.\nراهنما: /help"
        )
        return
    await show_main(update, ctx)


async def cmd_menu(update, ctx):
    if not await core.gate(update, ctx):
        return
    ctx.user_data.pop("await", None)
    await show_main(update, ctx)


async def cmd_help(update, ctx):
    if not await core.gate(update, ctx):
        return
    await update.effective_message.reply_text(core.HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_clear(update, ctx):
    if not await core.gate(update, ctx):
        return
    db.clear_history(update.effective_chat.id)
    ctx.user_data.pop("await", None)
    await update.effective_message.reply_text("🧹 حافظه‌ی گفتگو پاک شد؛ از اول شروع می‌کنیم.")


async def cmd_cancel(update, ctx):
    ctx.user_data.pop("await", None)
    await update.effective_message.reply_text("لغو شد ✅")


async def cmd_tts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    text = cmd_args(update.effective_message)
    if not text and update.effective_message.reply_to_message:
        text = update.effective_message.reply_to_message.text or update.effective_message.reply_to_message.caption or ""
    if not text:
        await update.effective_message.reply_text("روش استفاده: /tts متنی که باید به صدا تبدیل شود")
        return
    if not await core.check_quota(update, "msgs"):
        return
    note = await update.effective_message.reply_text("🔊 در حال ساخت پاسخ صوتی…")
    try:
        async with core.Typing(ctx.bot, update.effective_chat.id, ChatAction.RECORD_VOICE):
            audio = await gemini.generate_speech(text)
        await ctx.bot.send_document(update.effective_chat.id, audio, filename="roxet-speech.wav",
                                    caption="🔊 پاسخ صوتی ساخته شد")
        db.incr(update.effective_user.id, "msgs")
    except Exception as exc:
        log.exception("TTS failed")
        await send_error(update, ctx, ai.classify_error(exc))
    finally:
        await _drop(note)


async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    prompt = cmd_args(update.effective_message)
    if not prompt:
        await update.effective_message.reply_text("روش استفاده: /video توضیح ویدیوی دلخواه")
        return
    if not await core.check_quota(update, "msgs"):
        return
    note = await update.effective_message.reply_text("🎬 در حال ساخت ویدیو با Gemini Omni؛ ممکن است چند دقیقه طول بکشد…")
    try:
        async with core.Typing(ctx.bot, update.effective_chat.id, ChatAction.UPLOAD_VIDEO):
            video = await gemini.generate_video(prompt)
        await ctx.bot.send_video(update.effective_chat.id, video, filename="roxet-video.mp4",
                                 caption=("🎬 " + prompt)[:900])
        db.incr(update.effective_user.id, "msgs")
    except Exception as exc:
        log.exception("video generation failed")
        await send_error(update, ctx, ai.classify_error(exc))
    finally:
        await _drop(note)


async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    me = await ctx.bot.get_me()
    uid = update.effective_user.id
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    await update.effective_message.reply_text(
        f"🎁 لینک دعوت اختصاصی تو:\\n{link}\\n\\n👥 دعوت موفق: {db.referral_count(uid)} نفر\\n"
        "🎉 به ازای هر دعوت موفق، ۳ درخواست رایگان روزانه اضافه می‌شود."
    )


async def cmd_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    prompt = cmd_args(update.effective_message)[:1200]
    if not prompt:
        row = db.get_user(update.effective_user.id)
        await update.effective_message.reply_text("پرامپت شخصی فعلی:\\n" + (row["personal_prompt"] or "تنظیم نشده") +
                                                  "\\n\\nبرای تغییر: /prompt دستور دلخواهت")
        return
    db.set_personal_setting(update.effective_user.id, "personal_prompt", prompt)
    await update.effective_message.reply_text("✅ دستور شخصی ذخیره شد.")


async def cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await core.gate(update, ctx):
        return
    lang = cmd_args(update.effective_message).strip().lower()
    allowed = {"auto": "auto", "خودکار": "auto", "fa": "فارسی", "فارسی": "فارسی",
               "en": "English", "انگلیسی": "English", "tr": "ترکی", "ترکی": "ترکی"}
    if lang not in allowed:
        await update.effective_message.reply_text("زبان: /language auto یا /language fa یا /language en یا /language tr")
        return
    db.set_personal_setting(update.effective_user.id, "response_language", allowed[lang])
    await update.effective_message.reply_text("✅ زبان پاسخ ذخیره شد.")


async def cmd_id(update, ctx):
    u, c = update.effective_user, update.effective_chat
    text = f"🆔 شناسه عددی تو: <code>{u.id}</code>"
    if c.type != ChatType.PRIVATE:
        text += f"\n💬 شناسه این گروه: <code>{c.id}</code>"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


TASKS = {
    # command: (instruction, default body when the user only replied to something, save to history)
    "ask": ("", "این را توضیح بده و به آن پاسخ بده.", True),
    "explain": ("به‌صورت ساده، گام‌به‌گام و با مثال توضیح بده:", "", False),
    "summarize": ("خلاصه کن (چند نکته‌ی کلیدی و یک جمع‌بندی کوتاه):", "", False),
    "translate": (
        "ترجمه کن (متن فارسی را به انگلیسی و متن غیرفارسی را به فارسی؛ اگر زبان مقصد خواسته شد همان؛ فقط ترجمه را بنویس):",
        "",
        False,
    ),
}


async def task_command(update, ctx, name: str):
    if not await core.gate(update, ctx):
        return
    msg = update.effective_message
    instruction, default_body, save = TASKS[name]
    body = cmd_args(msg)
    media, kind, quoted = None, None, None
    rep = msg.reply_to_message
    if rep:
        try:
            media, kind, _ = await extract_media(rep)
        except MediaError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        quoted = rep.text or rep.caption
    if not (body or quoted or media):
        usage = {
            "ask": "/ask سؤال شما",
            "explain": "/explain موضوع یا متن (یا روی یک پیام ریپلای کن)",
            "summarize": "/summarize متن (یا روی یک پیام/فایل ریپلای کن)",
            "translate": "/translate متن (یا روی یک پیام ریپلای کن)",
        }[name]
        await msg.reply_text(f"نحوه استفاده:\n{usage}")
        return
    if body and instruction:
        text = f"{instruction}\n\n{body}"
    else:
        text = body or instruction or default_body
    if not text.strip():
        text = default_body or DEFAULT_PROMPTS.get(kind, "این را بررسی کن.")
    if media and not body and not instruction:
        text = DEFAULT_PROMPTS.get(kind, text)
    await run_chat(update, ctx, text, media, kind, save=save, quoted=quoted)


async def cmd_ask(update, ctx):
    await task_command(update, ctx, "ask")


async def cmd_explain(update, ctx):
    await task_command(update, ctx, "explain")


async def cmd_summarize(update, ctx):
    await task_command(update, ctx, "summarize")


async def cmd_translate(update, ctx):
    await task_command(update, ctx, "translate")


async def cmd_img(update, ctx):
    if not await core.gate(update, ctx):
        return
    msg = update.effective_message
    prompt = cmd_args(msg)
    rep = msg.reply_to_message
    sources = None
    if rep:
        try:
            media, kind, _ = await extract_media(rep)
        except MediaError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        if kind == "photo":
            sources = [media[0][1]]
        if not prompt and (rep.text or rep.caption):
            prompt = rep.text or rep.caption
    if not prompt:
        if update.effective_chat.type == ChatType.PRIVATE:
            ctx.user_data["await"] = ("img",)
            await msg.reply_text("🎨 تصویری که می‌خواهی را توصیف کن (هرچه دقیق‌تر بهتر). برای لغو /cancel")
        else:
            await msg.reply_text("نحوه استفاده: /img توضیح تصویر")
        return
    await do_image(update, ctx, prompt, sources)


# ------------------------------------------------------------------- messages
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or update.effective_user is None:
        return
    chat = update.effective_chat
    private = chat.type == ChatType.PRIVATE

    if private and is_admin(update.effective_user.id) and await admin.consume_text(update, ctx):
        return
    if not await core.gate(update, ctx, quiet_in_groups=True):
        return

    text = msg.text.strip()
    quoted = None
    if private:
        state = ctx.user_data.get("await")
        if state:
            if state[0] == "img":
                ctx.user_data.pop("await", None)
                await do_image(update, ctx, text)
                return
            if state[0] == "edit_prompt":
                ctx.user_data.pop("await", None)
                await do_image(update, ctx, text, [state[1]])
                return
            if state[0] == "edit_photo":
                await msg.reply_text("📷 اول عکسی که می‌خواهی ویرایش شود را بفرست (یا /cancel).")
                return
    else:
        if not is_addressed(msg, ctx.bot):
            return
        text = strip_mention(text, ctx.bot)
        rep = msg.reply_to_message
        if rep and rep.from_user and rep.from_user.id != ctx.bot.id:
            quoted = rep.text or rep.caption
        if not text:
            text = "سلام! چطور می‌توانم کمکت کنم؟"
    await run_chat(update, ctx, text, quoted=quoted)


EDIT_RE = re.compile(r"^\s*(/edit(@\w+)?|ویرایش|edit)\b\s*[:：\-]?\s*", re.I)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or update.effective_user is None:
        return
    chat = update.effective_chat
    private = chat.type == ChatType.PRIVATE
    if private and is_admin(update.effective_user.id) and await admin.consume_text(update, ctx):
        return  # e.g. the admin is sending a broadcast with media
    if not await core.gate(update, ctx, quiet_in_groups=True):
        return
    caption = (msg.caption or "").strip()
    if not private:
        if not is_addressed(msg, ctx.bot):
            return
        caption = strip_mention(caption, ctx.bot)
    try:
        parts, kind, _ = await extract_media(msg)
    except MediaError as exc:
        await msg.reply_text(f"⚠️ {exc}")
        return
    if not parts:
        return

    state = ctx.user_data.get("await") if private else None
    wants_edit = kind == "photo" and ((state and state[0] == "edit_photo") or EDIT_RE.match(caption))
    if wants_edit:
        ctx.user_data.pop("await", None)
        instruction = EDIT_RE.sub("", caption, count=1).strip()
        if not instruction:
            ctx.user_data["await"] = ("edit_prompt", parts[0][1])
            await msg.reply_text("✏️ حالا بنویس چه تغییری روی این عکس بدهم؟ (مثلاً: پس‌زمینه را شبانه کن)")
            return
        await do_image(update, ctx, instruction, [parts[0][1]])
        return
    await run_chat(update, ctx, caption or DEFAULT_PROMPTS[kind], parts, kind)


# ------------------------------------------------------------------ callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    user = update.effective_user
    chat = update.effective_chat

    if data == "chk":
        channel = db.get_setting("force_channel")
        if channel and not await core.is_member(ctx.bot, channel, user.id):
            await q.answer("هنوز عضو کانال نشده‌ای ❌", show_alert=True)
            return
        await q.answer("خوش آمدی ✅")
        try:
            await q.message.delete()
        except TelegramError:
            pass
        if chat.type == ChatType.PRIVATE:
            await ctx.bot.send_message(
                chat.id, welcome_text(user), parse_mode=ParseMode.HTML,
                reply_markup=core.main_menu(user.id, bool(db.upsert_user(user.id, user.username, user.first_name)["web"])),
            )
        return

    if not await core.gate(update, ctx):
        return
    row = db.get_user(user.id)

    if data == "menu:invite":
        me = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user.id}"
        await q.answer()
        await core.safe_edit(q, f"🎁 <b>دعوت دوستان</b>\\n\\nلینک اختصاصی تو:\\n<code>{link}</code>\\n\\n"
                             f"👥 دعوت موفق: <b>{db.referral_count(user.id)}</b>\\n"
                             "برای هر دعوت موفق، ۳ درخواست رایگان روزانه به سهمیه‌ات اضافه می‌شود.",
                             core.back_menu())
    elif data == "menu:prefs":
        await q.answer()
        row = db.get_user(user.id)
        await core.safe_edit(q, "⚙️ <b>تنظیمات شخصی</b>\\n"
                             f"🌐 زبان: {esc(row['response_language'] or 'auto')}\\n"
                             f"🧬 پرامپت شخصی: {'تنظیم شده' if row['personal_prompt'] else 'ندارد'}\\n\\n"
                             "برای تغییر زبان از /language و برای تنظیم دستور شخصی از /prompt استفاده کن.",
                             core.back_menu())
    elif data.startswith("answer:"):
        action = data.split(":", 1)[1]
        previous = ctx.user_data.get("last_answer", "")
        if not previous:
            await q.answer("پاسخی برای ویرایش پیدا نشد.", show_alert=True)
            return
        instruction = {"continue": "این پاسخ را ادامه بده و نکات باقی‌مانده را کامل کن.",
                       "short": "پاسخ قبلی را کوتاه‌تر و خلاصه‌تر بازنویسی کن.",
                       "translate": "پاسخ قبلی را به انگلیسی ترجمه کن."}.get(action)
        await q.answer()
        await run_chat(update, ctx, f"{instruction}\\n\\nپاسخ قبلی:\\n{previous}", save=False)
    if data == "menu:main":
        ctx.user_data.pop("await", None)
        await q.answer()
        await show_main(update, ctx, edit=True)
    elif data == "menu:new":
        db.clear_history(chat.id)
        await q.answer("🧹 حافظه پاک شد؛ گفتگوی جدید شروع شد.", show_alert=False)
    elif data == "menu:img":
        ctx.user_data["await"] = ("img",)
        await q.answer()
        await core.safe_edit(
            q, "🎨 <b>ساخت تصویر</b>\nتصویری که می‌خواهی را توصیف کن (هرچه دقیق‌تر، بهتر).\n"
            "مثال: <i>گربه‌ای فضانورد روی ماه، سبک نقاشی رنگ روغن</i>", core.back_menu(),
        )
    elif data == "menu:edit":
        ctx.user_data["await"] = ("edit_photo",)
        await q.answer()
        await core.safe_edit(
            q, "✏️ <b>ویرایش عکس</b>\nعکسی که می‌خواهی ویرایش شود را بفرست. می‌توانی تغییر دلخواه را هم "
            "در کپشن عکس بنویسی؛ وگرنه بعد از ارسال می‌پرسم.", core.back_menu(),
        )
    elif data == "menu:persona":
        await q.answer()
        await core.safe_edit(
            q, "🎭 <b>شخصیت ربات</b>\nانتخاب کن ربات با چه نقش و لحنی جواب بدهد:", core.persona_menu(row["persona"])
        )
    elif data.startswith("persona:"):
        key = data.split(":", 1)[1]
        if key in core.PERSONAS:
            db.set_user_field(user.id, "persona", key)
            await q.answer(f"شخصیت: {core.PERSONAS[key][0]}")
            await core.safe_edit(
                q, "🎭 <b>شخصیت ربات</b>\nانتخاب کن ربات با چه نقش و لحنی جواب بدهد:", core.persona_menu(key)
            )
    elif data == "menu:web":
        new = 0 if row["web"] else 1
        db.set_user_field(user.id, "web", new)
        await q.answer("🌐 جستجوی وب " + ("روشن شد" if new else "خاموش شد"))
        await core.safe_edit(q, welcome_text(user), core.main_menu(user.id, bool(new)))
    elif data == "menu:me":
        await q.answer()
        await core.safe_edit(q, account_text(row, user.id), core.back_menu())
    elif data == "menu:help":
        await q.answer()
        await core.safe_edit(q, core.HELP_TEXT, core.back_menu())
    elif data == "img:again":
        prompt = ctx.user_data.get("last_img_prompt")
        await q.answer()
        if prompt:
            await do_image(update, ctx, prompt)
    elif data == "img:file":
        img = core.images.get(user.id)
        await q.answer()
        if img:
            await ctx.bot.send_document(chat.id, img, filename="roxet-ai.png")
        else:
            await ctx.bot.send_message(chat.id, "این تصویر دیگر در حافظه نیست؛ دوباره بسازش.")
    elif data == "img:edit":
        img = core.images.get(user.id)
        await q.answer()
        if img:
            ctx.user_data["await"] = ("edit_prompt", img)
            await ctx.bot.send_message(chat.id, "✏️ چه تغییری روی این تصویر بدهم؟ بنویس (یا /cancel).")
        else:
            await ctx.bot.send_message(chat.id, "این تصویر دیگر در حافظه نیست؛ عکس را دوباره بفرست.")
    else:
        await q.answer()


def account_text(row, uid: int) -> str:
    dm, di = db.get_int("daily_msgs", 50), db.get_int("daily_imgs", 5)
    unlimited = is_admin(uid)
    lim_m = "∞" if unlimited or dm <= 0 else str(dm)
    lim_i = "∞" if unlimited or di <= 0 else str(di)
    persona = core.PERSONAS.get(row["persona"], core.PERSONAS["default"])[0]
    return (
        "👤 <b>حساب من</b>\n\n"
        f"🆔 شناسه: <code>{row['id']}</code>\n"
        f"📅 تاریخ امروز: {today_jalali()}\n"
        f"💬 پیام امروز: {row['msgs_today']} / {lim_m}\n"
        f"🎨 تصویر امروز: {row['imgs_today']} / {lim_i}\n"
        f"📈 مجموع: {row['total_msgs']} پیام، {row['total_imgs']} تصویر\n"
        f"🎭 شخصیت: {persona}\n"
        f"🌐 جستجوی وب: {'روشن' if row['web'] else 'خاموش'}"
    )


# ------------------------------------------------------------- group tracking
async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ch = update.my_chat_member
    chat = ch.chat
    new = ch.new_chat_member.status
    if chat.type == ChatType.PRIVATE:
        if new == "kicked":
            db.set_user_field(ch.from_user.id, "blocked", 1)
        return
    if new in ("member", "administrator"):
        g = db.upsert_group(chat.id, chat.title or "")
        who = ch.from_user
        await core.notify_admins(
            ctx.bot,
            f"➕ ربات به گروه <b>{esc(chat.title)}</b> اضافه شد.\n"
            f"👤 توسط: {esc(core.display_name(who))} (<code>{who.id}</code>)\n🆔 گروه: <code>{chat.id}</code>",
        )
        if db.get_int("groups_enabled", 1) and g["enabled"]:
            try:
                await ctx.bot.send_message(
                    chat.id,
                    f"سلام! من <b>{esc(config.BOT_NAME)}</b> هستم 🤖\n"
                    "• <code>/ask سؤال</code> — پرسش از هوش مصنوعی\n"
                    "• منشن کردن من یا ریپلای روی پیام‌هایم — گفتگو\n"
                    "• ریپلای روی یک پیام + <code>/explain</code> یا <code>/summarize</code> یا <code>/translate</code>\n"
                    "• <code>/img توضیح</code> — ساخت تصویر\n• /help — راهنمای کامل",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
    elif new in ("left", "kicked"):
        db.deactivate_group(chat.id)


async def on_inline_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Inline updates have no effective chat/message, so perform the equivalent
    # private-user checks here before spending Gemini API quota.
    inline_user = update.effective_user
    if not inline_user or inline_user.is_bot:
        await update.inline_query.answer([], cache_time=1, is_personal=True)
        return
    row = db.upsert_user(inline_user.id, inline_user.username, inline_user.first_name)
    if row["banned"] and not core.is_admin(inline_user.id):
        results = [InlineQueryResultArticle(
            id="banned", title="دسترسی شما مسدود است",
            input_message_content=InputTextMessageContent("دسترسی شما به این ربات مسدود شده است."),
        )]
        await update.inline_query.answer(results, cache_time=1, is_personal=True)
        return
    if db.get_int("maintenance", 0) and not core.is_admin(inline_user.id):
        results = [InlineQueryResultArticle(
            id="maintenance", title="ربات موقتاً در حال تعمیر است",
            input_message_content=InputTextMessageContent("ربات موقتاً در حال تعمیر است."),
        )]
        await update.inline_query.answer(results, cache_time=1, is_personal=True)
        return
    query = (update.inline_query.query or "").strip()
    if len(query) < 2:
        await update.inline_query.answer([], cache_time=2, is_personal=True)
        return
    try:
        row = db.get_user(update.effective_user.id)
        if row is None:
            row = db.upsert_user(update.effective_user.id, update.effective_user.username,
                                 update.effective_user.first_name)
        bonus = db.referral_count(update.effective_user.id) * 3
        limit = row["custom_daily_msgs"] if row["custom_daily_msgs"] > 0 else db.get_int("daily_msgs", 0)
        if limit > 0:
            limit += bonus
        if not core.is_admin(update.effective_user.id) and limit > 0 and row["msgs_today"] >= limit:
            results = [InlineQueryResultArticle(
                id="quota", title="سهمیه امروز تمام شده",
                input_message_content=InputTextMessageContent("سهمیه روزانه شما تمام شده است.")
            )]
            await update.inline_query.answer(results, cache_time=2, is_personal=True)
            return
        system = core.build_system(row["persona"], False, update.effective_user.id)
        answer = await gemini.chat([], [ai.text_part(query)], system, web=bool(row["web"]))
        results = [InlineQueryResultArticle(
            id="answer", title=answer[:90] or "پاسخ هوش مصنوعی",
            description="برای ارسال پاسخ در گفتگو انتخاب کن",
            input_message_content=InputTextMessageContent(answer[:4000])
        )]
        db.incr(update.effective_user.id, "msgs")
    except Exception as exc:
        log.warning("inline query failed: %s", exc)
        results = [InlineQueryResultArticle(
            id="error", title="خطا در دریافت پاسخ",
            input_message_content=InputTextMessageContent("فعلاً دریافت پاسخ ممکن نشد؛ دوباره تلاش کن.")
        )]
    await update.inline_query.answer(results, cache_time=1, is_personal=True)
