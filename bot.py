"""RoXeT AI — Telegram bot powered by Gemini.  Run:  python bot.py"""
import logging

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
)

import admin
import config
import core
import user

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("roxet")

PRIVATE_COMMANDS = [
    ("start", "شروع و منوی اصلی"),
    ("menu", "منوی دکمه‌ای"),
    ("img", "ساخت تصویر با هوش مصنوعی"),
    ("ask", "پرسیدن سؤال"),
    ("explain", "توضیح ساده یک موضوع/پیام"),
    ("summarize", "خلاصه‌سازی"),
    ("translate", "ترجمه"),
    ("clear", "پاک‌کردن حافظه گفتگو"),
    ("help", "راهنما"),
    ("id", "نمایش شناسه عددی"),
    ("invite", "لینک دعوت دوستان"),
    ("prompt", "تنظیم دستور شخصی"),
    ("language", "زبان پاسخ"),
    ("setquota", "تنظیم سهمیه اختصاصی کاربر"),
    ("addadmin", "افزودن مدیر"),
    ("deladmin", "حذف مدیر"),
    ("tts", "تبدیل متن به صدا"),
    ("video", "ساخت ویدیو با هوش مصنوعی"),
    ("addkey", "افزودن کلید API"),
    ("keys", "نمایش کلیدهای API"),
    ("delkey", "حذف کلید API"),
]
GROUP_COMMANDS = [
    ("ask", "پرسیدن سؤال از هوش مصنوعی"),
    ("explain", "توضیح پیام (ریپلای کن)"),
    ("summarize", "خلاصه‌سازی پیام (ریپلای کن)"),
    ("translate", "ترجمه پیام (ریپلای کن)"),
    ("img", "ساخت تصویر"),
    ("clear", "پاک‌کردن حافظه گروه"),
    ("help", "راهنما"),
]


def _cmds(items):
    return [BotCommand(c, d) for c, d in items]


async def post_init(app: Application):
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)
    await app.bot.set_my_commands(_cmds(PRIVATE_COMMANDS))
    await app.bot.set_my_commands(_cmds(GROUP_COMMANDS), scope=BotCommandScopeAllGroupChats())
    for uid in config.ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                _cmds(PRIVATE_COMMANDS + [("admin", "پنل مدیریت")]), scope=BotCommandScopeChat(uid)
            )
        except TelegramError:
            pass  # the admin has not started the bot yet
    if not core.gemini.has_key():
        await core.notify_admins(
            app.bot,
            "✅ ربات روشن شد.\n⚠️ هنوز کلید API Gemini ثبت نشده؛ /admin را بزن و «🔑 کلید API» را بزن.",
            key="boot",
            every=3600,
        )


async def on_error(update, context):
    log.error("Unhandled error", exc_info=context.error)


def main():
    config.validate()
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .post_init(post_init)
        .build()
    )
    for name, fn in [
        ("start", user.cmd_start), ("menu", user.cmd_menu), ("help", user.cmd_help),
        ("clear", user.cmd_clear), ("cancel", user.cmd_cancel), ("id", user.cmd_id),
        ("ask", user.cmd_ask), ("explain", user.cmd_explain), ("summarize", user.cmd_summarize),
        ("translate", user.cmd_translate), ("img", user.cmd_img), ("invite", user.cmd_invite),
        ("prompt", user.cmd_prompt), ("language", user.cmd_language), ("admin", admin.cmd_admin),
        ("setquota", admin.cmd_setquota), ("addadmin", admin.cmd_addadmin), ("deladmin", admin.cmd_deladmin),
        ("tts", user.cmd_tts), ("video", user.cmd_video),
        ("addkey", admin.cmd_addkey), ("keys", admin.cmd_keys), ("delkey", admin.cmd_delkey),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(admin.on_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(user.on_callback, pattern=r"^(menu:|persona:|img:|answer:|chk$)"))
    app.add_handler(InlineQueryHandler(user.on_inline_query))
    app.add_handler(ChatMemberHandler(user.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user.on_text))
    media = (
        filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Document.ALL | filters.Sticker.STATIC
    )
    app.add_handler(MessageHandler(media, user.on_media))
    app.add_error_handler(on_error)

    log.info("%s is starting…", config.BOT_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
