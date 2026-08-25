import os
import re
import random
import asyncio
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, ReplyParameters
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

# NEW: media helpers (download + convert). Lives in berimor_media.py next to this file.
from berimor_media import download_clip, convert_to_mp3, MediaError, DEFAULT_FADE_SEC

# Config
BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Asia/Yerevan")  # Armenia time (UTC+4)
PERSON = "Kre"                 # display name in replies

# How long the bot keeps waiting for a date after asking "erb?"
PENDING_TTL = timedelta(minutes=10)

# Anchor = a known DAY-shift date. Cycle is [Day, Night, Off, Off].
ANCHOR = date(2026, 8, 4)

DAY, NIGHT, OFF = "day", "night", "off"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("berimor")

# Only one heavy download/encode job at a time (safe for small dynos).
# Raise to Semaphore(2) if your host has spare CPU/RAM.
JOB_SEMAPHORE = asyncio.Semaphore(1)

# Telegram Bot API upload limit for regular bots.
TELEGRAM_MAX_MB = 50


def shift_for(d: date) -> str:
    pos = (d - ANCHOR).days % 4  # Python % is always >= 0
    if pos == 0:
        return DAY
    if pos == 1:
        return NIGHT
    return OFF

# Keyword matching
# name roots (kre/kren/krem/kremush/kremy, kar/karo/karapet, atabek/atabekyan)
NAME_RE = re.compile(r"\b(kre|kar|atabek|kapat|chax|chagh|karabas|barabas|կապատ|չաղ|կարաբաս|կառաբաս|բարաբաս|բառաբաս|կրե|կար|աթաբեկ)\w*", re.IGNORECASE)
# work words: gorc(i), ashxat(anq/el/um), smen, airport, zbaghvats (busy)...
WORK_RE = re.compile(r"(gorc|gordz|gorts|ashxat|ashkhat|smen|airport|aeroport|zbaghvats|zbaghvac|zbaxvac|աերոպորտ|օդանավ|գորձ|գործ|աշխատ|սմեն|զբաղված)", re.IGNORECASE)
# free word: azat / ազատ
AZAT_RE = re.compile(r"(azat|ազատ)", re.IGNORECASE)

# summon words / bot tag -> acknowledge
SUMMON_RE = re.compile(r"(berimor|բերիմոր|բերիմոռ|բեռիմոր|բեռիմոռ|@berimor_kre_shift_bot)", re.IGNORECASE)
SUMMON_REPLIES = [
    "այո, սըր",
    "լսում եմ, սըր",
    "լսում և հնազանդվում եմ, օ՜ իմ տիրակալ",
]

# insult words -> insult back with "<word> berimor"
INSULT_RE = re.compile(
    r"(himar|tavar|anasun|takanq|vaxkot|vakhkot|hambal|"
    r"hin ev cer|hin ev tser|hin u cer|hin u tser|"
    r"հիմար|տավար|անասուն|տականք|վախկոտ|համբալ|հին և ծեր|հին ու ծեր)",
    re.IGNORECASE,
)
INSULT_REPLIES = [
    "հիմար բերիմոր",
    "տավար բերիմոր",
    "անասուն բերիմոր",
    "տականք բերիմոր",
    "վախկոտ բերիմոր",
    "համբալ բերիմոր",
    "հին և ծեր բերիմոր",
]

# Armenian weekdays -> Python weekday() (Mon=0 … Sun=6)
WEEKDAYS = {
    "erkushabti": 0, "երկուշաբթի": 0,
    "ereqshabti": 1, "երեքշաբթի": 1,
    "chorekshabti": 2, "չորեքշաբթի": 2,
    "hingshabti": 3, "հինգշաբթի": 3,
    "urbat": 4, "ուրբաթ": 4,
    "shabat": 5, "շաբաթ": 5,
    "kiraki": 6, "կիրակի": 6,
}

# Month name (root) -> month number. Prefix match, so "dektemberi" matches "dektember".
MONTHS = {
    "hunvar": 1, "հունվար": 1,
    "petrvar": 2, "petervar": 2, "փետրվար": 2,
    "mart": 3, "մարտ": 3,
    "april": 4, "ապրիլ": 4,
    "mayis": 5, "մայիս": 5,
    "hunis": 6, "հունիս": 6,
    "hulis": 7, "հուլիս": 7,
    "ogostos": 8, "օգոստոս": 8,
    "september": 9, "septem": 9, "սեպտեմբեր": 9,
    "hoktember": 10, "hoktem": 10, "հոկտեմբեր": 10,
    "noyember": 11, "noyem": 11, "նոյեմբեր": 11,
    "dektember": 12, "dektem": 12, "դեկտեմբեր": 12, "դեկտեմ": 12,
}

def next_weekday(today: date, wd: int) -> date:
    diff = (wd - today.weekday()) % 7  # 0 == today
    return today + timedelta(days=diff)

def resolve_date(text: str, today: date):
    # month name + day  e.g. "dektemberi 26in" -> December 26
    for name, mon in MONTHS.items():
        if re.search(rf"\b{name}", text):
            dm = re.search(r"\b(\d{1,2})", text)
            if not dm:
                return None  # month said but no day -> ask when
            day = int(dm.group(1))
            year = today.year
            if mon < today.month:   # month already passed -> assume next year
                year += 1
            try:
                d = date(year, mon, day)
                return d, f"{day:02d}.{mon:02d}.{year}"
            except ValueError:
                return None

    # "amsi 12" -> day 12 of current month
    m = re.search(r"amsi\s+(\d{1,2})", text)
    if m:
        day = int(m.group(1))
        try:
            return today.replace(day=day), f"amsi {day}-in"
        except ValueError:
            return None

    # weekday name -> nearest upcoming occurrence (incl. today)
    for key, wd in WEEKDAYS.items():
        if re.search(rf"\b{key}", text):
            return next_weekday(today, wd), key

    # today (esor / aysor / էsor / այսօր)
    if re.search(r"(esor|aysor|hima|hmi|էսօր|այսօր|հիմա|հմի)", text):
        return today, "esor"
    # tomorrow (vaghy / vagy / vaxy / վաղը)
    if re.search(r"(vag|vax|վաղ)", text):
        return today + timedelta(days=1), "vaghy"
    # yesterday (erek / էrek / երեկ / էրեկ)
    if re.search(r"(erek|ereg|երեկ|էրեկ|երեգ|էրէգ)", text):
        return today - timedelta(days=1), "erek"

    # bare day number -> that day of the CURRENT month  e.g. "26in"
    dm = re.search(r"\b(\d{1,2})", text)
    if dm:
        day = int(dm.group(1))
        try:
            return today.replace(day=day), f"amsi {day}-in"
        except ValueError:
            return None

    return None  # nothing found

def render(s: str, label: str, d: date, mode: str) -> str:
    ds = d.strftime("%d.%m.%Y")
    if mode == "free":
        if s == OFF:
            return "հա ազատ ա ցավդ տանեմ"
        return "չէ զբաղված ա ախպերս"
    # work mode
    if s == DAY:
        return "հա ցավդ տանեմ, ցերեկվա սմեն"
    if s == NIGHT:
        return "հա ընգերս, գիշերային պախատ"
    return "չէ բռատս, ազատ ա"

def answer(target: date, label: str, mode: str) -> str:
    return render(shift_for(target), label, target, mode)

def quote_params(message, pattern):
    """Build ReplyParameters that quote the exact keyword `pattern` matched in `message.text`."""
    original = message.text or ""
    m = pattern.search(original)
    if not m:
        return None
    quoted = original[m.start():m.end()]
    # Telegram offsets are counted in UTF-16 code units, not Python code points.
    position = len(original[:m.start()].encode("utf-16-le")) // 2
    return ReplyParameters(
        message_id=message.message_id,
        quote=quoted,
        quote_position=position,
    )

# ===========================================================================
# NEW: /clip — download a YouTube section as MP4 (1080p, fade) + MP3
# ===========================================================================
CLIP_USAGE = (
    "🎬 *Clip downloader*\n\n"
    "Send:\n"
    "`/clip <youtube-url> <start> <end> [fade]`\n\n"
    "Times can be `M:SS`, `H:MM:SS`, or plain seconds.\n\n"
    "Examples:\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51`\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51 1`   (1s fade)\n\n"
    "Old 4-number style also works:\n"
    "`/clip <url> 0 31 0 51`   (start_min start_sec end_min end_sec)"
)


def parse_timecode(token: str) -> int:
    """'0:31' -> 31, '1:02:03' -> 3723, '45' -> 45 seconds. Raises ValueError."""
    token = token.strip()
    if not re.fullmatch(r"\d{1,2}(:\d{1,2}){0,2}", token):
        raise ValueError(f"bad time '{token}'")
    parts = [int(p) for p in token.split(":")]
    if len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        h, m, s = parts
    if m > 59 or s > 59:
        raise ValueError(f"minutes/seconds must be 0..59 in '{token}'")
    return h * 3600 + m * 60 + s


def parse_clip_args(args: list[str]) -> tuple[str, int, int, float]:
    """
    Accepts, after the /clip command:
        <url> <start> <end> [fade]                (timecode style)
        <url> <s_min> <s_sec> <e_min> <e_sec> [fade]   (old 4-number style)
    Returns (url, start_sec, end_sec, fade). Raises ValueError with a message.
    """
    if len(args) < 3:
        raise ValueError("Not enough arguments.")
    url = args[0]
    rest = args[1:]

    if len(rest) in (2, 3):            # timecode style
        start = parse_timecode(rest[0])
        end = parse_timecode(rest[1])
        fade = float(rest[2]) if len(rest) == 3 else DEFAULT_FADE_SEC
    elif len(rest) in (4, 5):          # old 4-number style
        s_min, s_sec, e_min, e_sec = (int(rest[0]), int(rest[1]), int(rest[2]), int(rest[3]))
        if not (0 <= s_sec < 60 and 0 <= e_sec < 60):
            raise ValueError("seconds must be 0..59")
        start = s_min * 60 + s_sec
        end = e_min * 60 + e_sec
        fade = float(rest[4]) if len(rest) == 5 else DEFAULT_FADE_SEC
    else:
        raise ValueError("Wrong number of arguments.")

    if fade < 0:
        raise ValueError("fade must be >= 0")
    if end <= start:
        raise ValueError("End time must be after start time.")
    return url, start, end, fade


async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    try:
        url, start, end, fade = parse_clip_args(context.args)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {e}\n\n{CLIP_USAGE}", parse_mode="Markdown"
        )
        return

    status = await update.message.reply_text("⏳ Resolving the video…")
    workdir = Path(tempfile.mkdtemp(prefix="berimor_clip_"))

    async with JOB_SEMAPHORE:
        try:
            # 1) Download + cut + fade + normalize to 1080p (blocking -> thread)
            await status.edit_text("⬇️ Downloading & encoding 1080p… this can take a bit.")
            result = await asyncio.to_thread(
                download_clip, url, start, end, workdir, fade=fade
            )

            # Warn if YouTube only served a sub-1080p source (usually needs PO token)
            if result.height and result.height < 1080:
                await update.message.reply_text(
                    f"ℹ️ YouTube only served {result.width}x{result.height} for this "
                    "video (likely needs a PO token / cookies on the server), so the "
                    "MP4 is padded to 1080p rather than truly 1080p."
                )

            # 2) Convert to MP3 (blocking -> thread)
            await status.edit_text("🎧 Converting to MP3…")
            mp3_path = await asyncio.to_thread(convert_to_mp3, result.mp4_path)

            # 3) Send both files back
            await status.edit_text("📤 Uploading files…")
            mp4_mb = result.mp4_path.stat().st_size / (1024 * 1024)

            # ---- MP4 ----
            if mp4_mb > TELEGRAM_MAX_MB:
                await update.message.reply_text(
                    f"⚠️ The MP4 is {mp4_mb:.0f} MB, above Telegram's "
                    f"{TELEGRAM_MAX_MB} MB bot limit, so I can't send it here. "
                    "Try a shorter range. Sending the MP3 only."
                )
            else:
                with open(result.mp4_path, "rb") as fh:
                    await update.message.reply_document(
                        document=fh,
                        filename=result.mp4_path.name,
                        caption=f"🎬 {result.title}",
                        read_timeout=600, write_timeout=600, connect_timeout=60,
                    )

            # ---- MP3 ----
            with open(mp3_path, "rb") as fh:
                await update.message.reply_audio(
                    audio=fh,
                    filename=mp3_path.name,
                    title=result.title,
                    read_timeout=600, write_timeout=600, connect_timeout=60,
                )

            await status.delete()

        except MediaError as e:
            log.warning("clip failed: %s", e)
            await status.edit_text(f"❌ {e}")
        except Exception as e:  # noqa: BLE001 - report anything unexpected
            log.exception("unexpected clip error")
            await status.edit_text(f"❌ Unexpected error: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(CLIP_USAGE, parse_mode="Markdown")


# Handler
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.lower()
    today = datetime.now(TZ).date()

    # A real question needs a name + (a work word OR the free word "azat").
    has_name = bool(NAME_RE.search(text))
    has_azat = bool(AZAT_RE.search(text))
    has_work = bool(WORK_RE.search(text))
    is_question = has_name and (has_work or has_azat)
    # if "azat" is present, answer in free/busy wording
    mode = "free" if has_azat else "work"

    if is_question:
        result = resolve_date(text, today)
        if result is None:
            # no date given: ask "when?" and start waiting (remember the mode)
            context.chat_data["awaiting_since"] = datetime.now(TZ)
            context.chat_data["awaiting_mode"] = mode
            await update.message.reply_text("ե՞րբ ցավդ տանեմ")
            return
        # date given: answer and stop waiting
        context.chat_data.pop("awaiting_since", None)
        context.chat_data.pop("awaiting_mode", None)
        target, label = result
        await update.message.reply_text(answer(target, label, mode))
        return

    # not a full question: maybe it's the answer to a previous "erb?"
    awaiting = context.chat_data.get("awaiting_since")
    if awaiting and datetime.now(TZ) - awaiting <= PENDING_TTL:
        result = resolve_date(text, today)
        if result is not None:
            saved_mode = context.chat_data.get("awaiting_mode", "work")
            context.chat_data.pop("awaiting_since", None)
            context.chat_data.pop("awaiting_mode", None)
            target, label = result
            await update.message.reply_text(answer(target, label, saved_mode))
            return

    # insult -> reply, quoting the exact keyword that triggered it (1/7 each)
    if INSULT_RE.search(text):
        reply = random.choice(INSULT_REPLIES)
        rp = quote_params(update.message, INSULT_RE)
        try:
            await update.message.reply_text(reply, reply_parameters=rp)
        except Exception:
            # if the partial-quote is rejected, fall back to a normal reply
            await update.message.reply_text(reply)
        return

    # summon (word or @tag) -> random acknowledgement (1/3 each)
    if SUMMON_RE.search(text):
        await update.message.reply_text(random.choice(SUMMON_REPLIES))
        return

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # NEW: commands (these never reach the schedule handler, which excludes commands)
    app.add_handler(CommandHandler("clip", clip_command))
    app.add_handler(CommandHandler(["start", "help"], start_command))

    # Existing schedule / banter handler (unchanged)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("Bot runnin'")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
