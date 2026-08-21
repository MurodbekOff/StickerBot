"""
fstik-style sticker pack manager + IG/TikTok downloader bot.

Commands:
  /start        - greeting + menu (also handles co-edit share links)
  /newpack      - create a new sticker set
  /addsticker   - add to an existing pack
  /mypacks      - list your packs; tap one for Add/Rename/Co-edit
  /done         - finish the current pack-editing session
  /cancel       - abort the current operation
  /caption on|off - toggle the credit caption on downloaded videos

Once you're "editing" a pack (after /newpack, tapping Add on a pack, or
opening someone else's co-edit link), just send images, GIFs, videos, or
static/video stickers one after another -- each is added immediately with a
default 😭 emoji. Send emoji right after a sticker to retag it instead.
GIFs/videos are auto-converted (via ffmpeg) into Telegram's video-sticker
format; Lottie/.tgs animated stickers still aren't supported.

Paste an Instagram or TikTok link at any time (no command needed) and the bot
downloads and re-sends the video.

Requires: python-telegram-bot>=21.0, Pillow>=10.0, yt-dlp>=2024.1, ffmpeg on PATH
Env vars: BOT_TOKEN, BOT_USERNAME (no @)
"""
import asyncio
import logging
import os
import re
import tempfile

try:  # optional convenience: load BOT_TOKEN/BOT_USERNAME from a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from db import (
    init_db,
    add_pack,
    get_user_packs,
    get_pack_owner,
    get_pack_title,
    set_pack_title,
    get_caption_enabled,
    set_caption_enabled,
    add_editor,
    get_editor_ids,
    get_or_create_share_token,
    reset_share_token,
    get_pack_by_token,
)
from image_utils import to_sticker_png
from video_sticker import to_video_sticker_webm, ConversionError
from emoji_utils import DEFAULT_EMOJI, looks_like_emoji_message, split_emoji
from video import LINK_RE, find_link, download_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")  # no @

TITLE, EDITING, RENAME = range(3)

HELP_TEXT = (
    "Commands:\n"
    "/newpack - start a new sticker pack\n"
    "/addsticker - add stickers to an existing pack\n"
    "/mypacks - list your packs\n"
    "/done - finish editing a pack\n"
    "/cancel - stop whatever you're doing\n"
    "/caption on|off - toggle the credit caption on downloaded videos\n\n"
    "While editing a pack: send images, GIFs, videos, or static/video "
    "stickers to add them.\n"
    "Send emoji after one to tag it with that emoji.\n\n"
    "Tap a pack from /mypacks to rename it or set up co-editing so someone "
    "else can add stickers to it too.\n\n"
    "This bot is still being developed and hosted temporarily. If its not responding, "
    "wait for it to respond, it will automatically respond when I start hosting it again most of the time.\n\n"
)


# ---------- small helpers ----------

async def reply(update: Update, text: str, **kwargs):
    """Works whether the update came from a command or a button tap."""
    if update.message:
        await update.message.reply_text(text, **kwargs)
    else:
        await update.callback_query.message.reply_text(text, **kwargs)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug[:30] or "pack"


def start_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ New pack", callback_data="menu_newpack"),
                InlineKeyboardButton("📁 My packs", callback_data="menu_mypacks"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="menu_help")],
        ]
    )


def packs_keyboard(packs: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """One button per pack (tap opens its menu), fStik-style."""
    rows = [[InlineKeyboardButton(title, callback_data=f"packopen:{name}")] for name, title in packs]
    rows.append([InlineKeyboardButton("➕ New pack", callback_data="menu_newpack")])
    return InlineKeyboardMarkup(rows)


def pack_detail_keyboard(pack_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Open pack", url=f"https://t.me/addstickers/{pack_name}")],
            [InlineKeyboardButton("➕ Add stickers", callback_data=f"pack:{pack_name}")],
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"packrename:{pack_name}"),
                InlineKeyboardButton("👥 Co-edit", callback_data=f"packcoedit:{pack_name}"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu_mypacks")],
        ]
    )


def _owns_pack_or_deny(pack_name: str, user_id: int) -> bool:
    """True if user_id owns pack_name. Packs can only be managed (renamed,
    shared, or opened via the /mypacks menu) by their owner -- co-editors
    only ever get add access, and only through a valid share link."""
    return get_pack_owner(pack_name) == user_id


# ---------- /start and menu buttons ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("s_"):
        return await start_coedit_link(update, context, args[0][2:])

    await update.message.reply_text(
        "Hey! I turn your images/GIFs/videos/stickers into Telegram sticker packs, "
        "and I can grab videos from Instagram/TikTok links.\n\n" + HELP_TEXT,
        reply_markup=start_menu_keyboard(),
    )
    return ConversationHandler.END


async def start_coedit_link(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Handles /start s_<token> -- someone opening a co-edit share link."""
    pack_name = get_pack_by_token(token)
    if not pack_name:
        await update.message.reply_text(
            "That co-editing link isn't valid -- it may have been reset by the pack owner."
        )
        return ConversationHandler.END

    owner_id = get_pack_owner(pack_name)
    user = update.effective_user
    if owner_id is None:
        await update.message.reply_text("That pack doesn't seem to exist anymore.")
        return ConversationHandler.END
    if owner_id == user.id:
        await update.message.reply_text("That's your own pack -- use /mypacks to manage it.")
        return ConversationHandler.END

    add_editor(pack_name, user.id)
    title = get_pack_title(pack_name) or pack_name

    context.user_data.clear()
    context.user_data["mode"] = "add"
    context.user_data["pack_created"] = True
    context.user_data["target_pack"] = pack_name
    context.user_data["owner_id"] = owner_id

    await update.message.reply_text(
        f"You've been added as a co-editor on \"{title}\"! Send images, GIFs, "
        "videos, or static/video stickers to add them -- default emoji is 😭, "
        "send emoji right after to retag the last one. /done when finished."
    )
    return EDITING


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(HELP_TEXT)


async def menu_mypacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_packs(update, context)


async def mypacks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_packs(update, context)


async def show_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    packs = get_user_packs(user_id)
    if not packs:
        await reply(update, "No packs yet -- tap New pack or use /newpack.")
        return
    await reply(update, "Your packs:", reply_markup=packs_keyboard(packs))


# ---------- pack detail menu (Add / Rename / Co-edit) ----------

async def pack_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pack_name = query.data.split(":", 1)[1]
    user = update.effective_user

    if not _owns_pack_or_deny(pack_name, user.id):
        await query.answer("That's not your pack.", show_alert=True)
        return
    await query.answer()

    title = get_pack_title(pack_name) or pack_name
    await query.message.reply_text(f"📦 {title}", reply_markup=pack_detail_keyboard(pack_name))


# ---------- co-editing menu ----------

async def _send_coedit_message(message, pack_name: str):
    token = get_or_create_share_token(pack_name)
    title = get_pack_title(pack_name) or pack_name
    link = f"https://t.me/{BOT_USERNAME}?start=s_{token}"
    editor_count = len(get_editor_ids(pack_name))
    editors_line = f"{editor_count} co-editor(s) so far." if editor_count else "No co-editors yet."

    text = (
        f"👥 Co-editing \"{title}\"\n\n"
        f"Link: {link}\n\n"
        "Share it -- anyone who opens it can add stickers to this pack "
        "through the bot (they still get added under your ownership).\n\n"
        f"{editors_line}\n\n"
        "Reset the link to stop it from granting access to anyone new."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Reset link", callback_data=f"cotoken_reset:{pack_name}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"packopen:{pack_name}")],
        ]
    )
    await message.reply_text(text, reply_markup=kb)


async def coedit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pack_name = query.data.split(":", 1)[1]
    user = update.effective_user

    if not _owns_pack_or_deny(pack_name, user.id):
        await query.answer("Only the pack owner can manage co-editing.", show_alert=True)
        return
    await query.answer()
    await _send_coedit_message(query.message, pack_name)


async def coedit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pack_name = query.data.split(":", 1)[1]
    user = update.effective_user

    if not _owns_pack_or_deny(pack_name, user.id):
        await query.answer("Only the pack owner can manage co-editing.", show_alert=True)
        return

    reset_share_token(pack_name)
    await query.answer("Link reset -- the old one no longer works.")
    await _send_coedit_message(query.message, pack_name)


# ---------- rename ----------

async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pack_name = query.data.split(":", 1)[1]
    user = update.effective_user

    if not _owns_pack_or_deny(pack_name, user.id):
        await query.answer("Only the pack owner can rename it.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data.clear()
    context.user_data["rename_pack"] = pack_name
    await query.message.reply_text(
        f"Send the new title for \"{get_pack_title(pack_name) or pack_name}\"."
    )
    return RENAME


async def receive_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pack_name = context.user_data.get("rename_pack")
    new_title = update.message.text.strip()[:64]  # Telegram title limit
    if not pack_name:
        await update.message.reply_text("Something went wrong -- try Rename again from /mypacks.")
        return ConversationHandler.END

    try:
        await context.bot.set_sticker_set_title(name=pack_name, title=new_title)
        set_pack_title(pack_name, new_title)
        await update.message.reply_text(f"Renamed to \"{new_title}\".")
    except Exception as exc:
        logger.exception("Rename failed")
        await update.message.reply_text(f"Couldn't rename it: {exc}")

    context.user_data.clear()
    return ConversationHandler.END


# ---------- /newpack (and "New pack" button) ----------

async def newpack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "new"
    context.user_data["pack_created"] = False
    context.user_data["owner_id"] = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    await reply(update, "What should the pack title be?")
    return TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send images, GIFs, videos, or static/video stickers -- each one "
        "is added with the default 😭 emoji. Send emoji right after to retag "
        "the last one. /done when finished."
    )
    return EDITING


# ---------- /addsticker (and per-pack "Add" button) ----------

async def addsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    packs = get_user_packs(user_id)
    if not packs:
        await update.message.reply_text("You don't have any packs yet. Use /newpack first.")
        return
    await update.message.reply_text(
        "Which pack? Tap it, then \"➕ Add stickers\".", reply_markup=packs_keyboard(packs)
    )


async def pack_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pack_name = query.data.split(":", 1)[1]
    user = update.effective_user

    owner_id = get_pack_owner(pack_name)
    if owner_id != user.id:
        await query.answer("That's not your pack.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    context.user_data.clear()
    context.user_data["mode"] = "add"
    context.user_data["pack_created"] = True
    context.user_data["target_pack"] = pack_name
    context.user_data["owner_id"] = owner_id

    await query.message.reply_text(
        "Send images, GIFs, videos, or static/video stickers to add -- default "
        "emoji is 😭, send emoji right after to retag the last one. /done when finished."
    )
    return EDITING


# ---------- the editing loop ----------

async def _add_input_sticker(context: ContextTypes.DEFAULT_TYPE, user, input_sticker: InputSticker) -> str:
    """Creates the pack (first sticker of a /newpack session) or adds to the
    existing target pack. Returns the newly-added sticker's file_id.

    Always acts on Telegram's behalf of the pack's *owner* (owner_id), even
    if a co-editor is the one physically sending the sticker through the
    bot -- Telegram's API ties set ownership to that id, not the caller.
    """
    owner_id = context.user_data["owner_id"]

    if not context.user_data.get("pack_created"):
        title = context.user_data["title"]
        pack_name = f"{slugify(title)}_{owner_id}_by_{BOT_USERNAME}"
        await context.bot.create_new_sticker_set(
            user_id=owner_id,
            name=pack_name,
            title=title,
            stickers=[input_sticker],
        )
        add_pack(owner_id, pack_name, title)
        context.user_data["target_pack"] = pack_name
        context.user_data["pack_created"] = True
    else:
        pack_name = context.user_data["target_pack"]
        await context.bot.add_sticker_to_set(
            user_id=owner_id,
            name=pack_name,
            sticker=input_sticker,
        )

    sticker_set = await context.bot.get_sticker_set(pack_name)
    return sticker_set.stickers[-1].file_id


async def receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Static images / static stickers -> PNG sticker."""
    msg = update.message
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document:
        file_id = msg.document.file_id
    elif msg.sticker:
        file_id = msg.sticker.file_id
    else:
        return EDITING

    tg_file = await context.bot.get_file(file_id)
    raw = await tg_file.download_as_bytearray()

    try:
        png_bytes = to_sticker_png(bytes(raw)).getvalue()
    except Exception as exc:
        await msg.reply_text(f"Couldn't process that image: {exc}")
        return EDITING

    input_sticker = InputSticker(sticker=png_bytes, emoji_list=[DEFAULT_EMOJI], format="static")

    try:
        last_id = await _add_input_sticker(context, update.effective_user, input_sticker)
        context.user_data["last_sticker_id"] = last_id
        await msg.reply_text(
            f"Added with default {DEFAULT_EMOJI}. Send emoji now to retag it, "
            "another image/GIF/video to keep going, or /done to finish."
        )
    except Exception as exc:
        logger.exception("Sticker set operation failed")
        await msg.reply_text(f"Something went wrong: {exc}")

    return EDITING


async def receive_video_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """GIFs / videos / video stickers -> WEBM video sticker (via ffmpeg)."""
    msg = update.message
    if msg.animation:
        file_id = msg.animation.file_id
    elif msg.video:
        file_id = msg.video.file_id
    elif msg.document:
        file_id = msg.document.file_id
    elif msg.sticker:
        file_id = msg.sticker.file_id
    else:
        return EDITING

    tg_file = await context.bot.get_file(file_id)
    raw = await tg_file.download_as_bytearray()

    status = await msg.reply_text("Converting to a video sticker...")
    try:
        webm_bytes = await asyncio.to_thread(to_video_sticker_webm, bytes(raw))
    except ConversionError as exc:
        await status.edit_text(str(exc))
        return EDITING
    except Exception as exc:
        logger.exception("Video conversion failed")
        await status.edit_text(f"Couldn't convert that: {exc}")
        return EDITING

    input_sticker = InputSticker(sticker=webm_bytes, emoji_list=[DEFAULT_EMOJI], format="video")

    try:
        last_id = await _add_input_sticker(context, update.effective_user, input_sticker)
        context.user_data["last_sticker_id"] = last_id
        await status.edit_text(
            f"Added as a video sticker with default {DEFAULT_EMOJI}. Send emoji "
            "now to retag it, another image/GIF/video to keep going, or /done to finish."
        )
    except Exception as exc:
        logger.exception("Sticker set operation failed")
        await status.edit_text(f"Something went wrong: {exc}")

    return EDITING


async def reject_unsupported_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Animated (Lottie/.tgs) stickers aren't supported -- send a static "
        "image, a GIF/video, or a static/video sticker instead."
    )
    return EDITING


async def maybe_emoji_override(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if not looks_like_emoji_message(text):
        await update.message.reply_text(
            "Send an image/GIF/video/sticker to add, emoji to retag the last one, or /done."
        )
        return EDITING

    last_sticker_id = context.user_data.get("last_sticker_id")
    if not last_sticker_id:
        await update.message.reply_text("Add a sticker first, then send emoji to tag it.")
        return EDITING

    emojis = split_emoji(text)
    try:
        await context.bot.set_sticker_emoji_list(sticker=last_sticker_id, emoji_list=emojis)
        await update.message.reply_text(f"Retagged as {' '.join(emojis)}.")
    except Exception as exc:
        await update.message.reply_text(f"Couldn't update the emoji: {exc}")

    return EDITING


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pack_name = context.user_data.get("target_pack")
    if not pack_name:
        await update.message.reply_text("You haven't added anything yet. Send an image first.")
        return EDITING
    await update.message.reply_text(f"All set: https://t.me/addstickers/{pack_name}")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------- Instagram / TikTok downloader (works anytime, no command) ----------

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = find_link(update.message.text)
    if not url:
        return

    status = await update.message.reply_text("Downloading...")
    path = None
    try:
        path = await asyncio.to_thread(download_video, url, tempfile.gettempdir())
        caption = None
        if get_caption_enabled(update.effective_user.id):
            caption = f"⬇️ via @{BOT_USERNAME}"
        with open(path, "rb") as f:
            await update.message.reply_video(f, caption=caption)
        await status.delete()
    except Exception as exc:
        logger.exception("Video download failed")
        await status.edit_text(f"Couldn't download that: {exc}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def caption_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = get_caption_enabled(update.effective_user.id)
        await update.message.reply_text(
            f"Caption is currently {'ON' if current else 'OFF'}. Use /caption on or /caption off."
        )
        return
    enabled = context.args[0].lower() == "on"
    set_caption_enabled(update.effective_user.id, enabled)
    await update.message.reply_text(f"Download caption turned {'ON' if enabled else 'OFF'}.")


def main():
    if not BOT_TOKEN or not BOT_USERNAME:
        raise SystemExit("Set BOT_TOKEN and BOT_USERNAME environment variables first.")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("newpack", newpack_start),
            CallbackQueryHandler(newpack_start, pattern="^menu_newpack$"),
            CallbackQueryHandler(pack_chosen, pattern="^pack:"),
            CallbackQueryHandler(rename_start, pattern="^packrename:"),
        ],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_title)],
            EDITING: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | filters.Sticker.STATIC,
                    receive_media,
                ),
                MessageHandler(
                    filters.ANIMATION | filters.VIDEO | filters.Document.VIDEO | filters.Sticker.VIDEO,
                    receive_video_media,
                ),
                MessageHandler(
                    filters.Sticker.ANIMATED,
                    reject_unsupported_sticker,
                ),
                CommandHandler("done", done),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(LINK_RE),
                    maybe_emoji_override,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(LINK_RE), handle_link))
    app.add_handler(CommandHandler("mypacks", mypacks_command))
    app.add_handler(CommandHandler("addsticker", addsticker_command))
    app.add_handler(CommandHandler("caption", caption_toggle))
    app.add_handler(CallbackQueryHandler(menu_mypacks, pattern="^menu_mypacks$"))
    app.add_handler(CallbackQueryHandler(menu_help, pattern="^menu_help$"))
    app.add_handler(CallbackQueryHandler(pack_detail, pattern="^packopen:"))
    app.add_handler(CallbackQueryHandler(coedit_menu, pattern="^packcoedit:"))
    app.add_handler(CallbackQueryHandler(coedit_reset, pattern="^cotoken_reset:"))

    logger.info("Bot starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()