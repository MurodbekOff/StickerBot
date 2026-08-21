# fstik-style sticker pack bot

## Setup

1. Talk to **@BotFather** on Telegram, `/newbot`, get a token and your bot's username.
2. Install **ffmpeg** on the host (`apt install ffmpeg` / `brew install ffmpeg` / a
   Windows build on PATH) -- it's required now, both for the IG/TikTok downloader
   and for converting GIFs/videos into video stickers.
3. `pip install -r requirements.txt`
4. Set env vars and run:
   ```
   export BOT_TOKEN="123456:ABC-your-token"
   export BOT_USERNAME="your_bot_username"   # no @
   python bot.py
   ```
5. In Telegram: `/newpack` → send title → send image/GIF/video → pick emoji → Done.

This uses **polling**, not webhooks — no public URL or open port needed.

## Hosting options

### Just your laptop (good enough to start)
Run `python bot.py` in a terminal, or from VS Code's built-in terminal/debugger.
Zero setup cost, but the bot only works while that process is running — close the
laptop lid or lose wifi and it's offline. Fine for solo use/testing. On Windows,
`pythonw bot.py` avoids a console window; on any OS, running it inside `tmux`/`screen`
(Linux/Mac) or just leaving the terminal open is enough.

### Replit
Works, with two caveats:
- Free Repls sleep after inactivity — pinging isn't needed for a *polling* bot
  since there's no incoming webhook, but the process itself gets killed when the
  container spins down, so the bot goes offline until you reopen it. Replit's
  "Always On" (paid) or their Deployments feature keeps it running continuously.
- Store `BOT_TOKEN` in Replit's **Secrets** tab, not in code.

Good for "spin it up in five minutes from a browser," not for guaranteed uptime.

### A free-tier cloud box (still temporary/light, but more stable than a laptop)
- **Railway** / **Render** / **Fly.io** — all have small free or very cheap tiers,
  deploy straight from a GitHub repo, keep the process running 24/7 without your
  laptop needing to be on. This is the natural next step once your laptop-based
  version works and you want it always available.
- **PythonAnywhere** — free tier can run "always-on tasks" (paid) or scheduled
  tasks; a bit more manual but works for small always-on Python scripts.

### A real VPS (cheap, once you outgrow "temporary")
Any $5/mo box (DigitalOcean, Hetzner, a spare Raspberry Pi at home, etc.) — install
Python, `pip install -r requirements.txt`, run under `systemd` or inside `screen`/`tmux`
so it survives SSH disconnects and reboots. Most durable option, minimal cost, full
control — the natural home for this once it's more than just-you-testing-it.

### What you don't need
Because this bot uses polling (not a webhook), you don't need HTTPS, a domain, or
an open inbound port anywhere — that's what makes laptop/Replit hosting viable at
all. Webhooks only become worth it at higher message volume (many concurrent users),
which isn't your case.

## Notes
- `fstik.db` (SQLite) tracks which packs belong to which user locally (plus
  co-editors and share-link tokens), and each user's caption on/off preference —
  delete it and your bot forgets pack ownership/co-editors (Telegram still keeps
  the actual packs) and resets everyone's caption setting.
- Images, static stickers, GIFs, videos, and video stickers are all supported.
  GIFs/videos get auto-converted (via ffmpeg, `video_sticker.py`) into Telegram's
  video-sticker spec: WEBM/VP9, one side exactly 512px, ≤3 seconds (longer clips
  get trimmed), no audio, ≤256 KB. It retries at lower quality/frame-rate if the
  first pass doesn't fit, and gives up with a clear message on a handful of
  genuinely incompressible clips (rare in practice for real memes/GIFs).
- Animated **Lottie/`.tgs`** stickers still aren't supported (that format needs a
  separate rendering pipeline, not just ffmpeg) and get politely rejected.
- The video downloader and the GIF/video-sticker converter both need **ffmpeg** on
  the host. `apt install ffmpeg` (Linux), `brew install ffmpeg` (Mac), or download
  a build for Windows and add it to PATH.
- "Inline buttons" here means the buttons attached under a message (what you saw in
  the fStikBot/MegaSaverBot screenshots) — that's Telegram's **inline keyboard**
  feature, which is what this bot uses throughout. That's different from Telegram's
  separate "inline mode" (typing `@yourbot query` in any chat's message box), which
  isn't implemented here — say the word if you want that too.

## How the new pieces work

- **`/start`** shows a greeting, the command list, and three buttons: New pack,
  My packs, Help. Buttons and commands trigger the same code paths. `/start` is
  also how co-edit share links work (see below).
- **`/mypacks`** now lists just your pack titles, fStik-style — tap one to open a
  menu with **Open pack**, **➕ Add stickers**, **✏️ Rename**, and **👥 Co-edit**.
  `/addsticker` uses the same picker/menu.
- **Editing sessions**: after `/newpack` (title first), tapping "Add stickers" on a
  pack, or opening a co-edit link, the bot stays in an "editing" mode. Send photos,
  image files, GIFs, videos, or static/video stickers one after another — each is
  uploaded immediately with the default 😭 emoji (GIFs/videos get converted to video
  stickers first, which takes a couple seconds). Send emoji right after one (e.g.
  `🔥` or `😂🔥`) to retag *the last one you sent* instead of adding a new sticker.
  `/done` wraps up and gives you the pack link; `/cancel` stops the session
  (stickers already added stay added).
- **Rename** sends `setStickerSetTitle` to Telegram and updates the locally-cached
  title, so it takes effect immediately in Telegram's own UI too.
- **Co-editing**: tapping "👥 Co-edit" shows a `t.me/yourbot?start=s_<token>` link.
  Anyone who opens it gets add-only access to that pack through the bot (they
  can't rename it, manage sharing, or see it in their own `/mypacks`) — stickers
  they add still appear under *your* Telegram sticker-set ownership, since
  Telegram ties a set's owner to the id used when it was created, not to whoever
  is currently adding to it. "🔄 Reset link" invalidates the old link (anyone who
  already has the new one keeps their access; this only stops the *old* link from
  granting access to someone new).
- **Link downloader**: paste any `instagram.com` or `tiktok.com` link in the chat —
  no command needed, and it works even mid-editing-session. `/caption on` / `off`
  toggles whether downloaded videos get a `⬇️ via @yourbot` caption.
