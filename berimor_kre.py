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
# name roots (kre/kren/krem/kremush/kremy, atabek/atabekyan, ...)
NAME_RE = re.compile(r"\b(kre|atabek|kapat|chax|chagh|karabas|barabas|կապատ|չաղ|կարաբաս|կառաբաս|բարաբաս|բառաբաս|կրե|աթաբեկ)\w*", re.IGNORECASE)
# work words: gorc(i), ashxat(anq/el/um), smen, airport, zbaghvats (busy)...
WORK_RE = re.compile(r"(gorc|gordz|gorts|ashxat|ashkhat|smen|airport|aeroport|zbaghvats|zbaghvac|zbaxvac|աերոպորտ|օդանավ|գորձ|գործ|աշխատ|սմեն|զբաղված)", re.IGNORECASE)
# free word: azat / ազատ
AZAT_RE = re.compile(r"(azat|ազատ)", re.IGNORECASE)

# "self" words (es / ես) -> stand in for the name, but ONLY for SELF_USER_ID.
# Must be a separate word: "ես" matches, "կնկարես" / "esor" do not.
# Also accepts the Armenian question/emphasis mark inside it: "ե՞ս", "ե՛ս".
SELF_USER_ID = 866680974
SELF_RE = re.compile(r"(?<!\w)(es|ե[՞՛՜]?ս)(?!\w)", re.IGNORECASE)

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

# cinema words -> reply "kinaciatr"
# prefix match, so "kinoyi", "filmy", "tatronum", "կինոյի", "թատրոնում" also trigger
KINO_RE = re.compile(
    r"\b(kino|kinotatron|kinaciatr|film|movie|theatre|theater|tatron|"
    r"կինո|կինոթատրոն|կինածիատր|ֆիլմ|մուվի|թատրոն)",
    re.IGNORECASE,
)
KINO_REPLY = "kinaciatr"

# Rare replies: 1 in 100 summons / insults get these instead of the lists above
RARE_CHANCE = 0.01
SUMMON_RARE_REPLY = "ինչա արա"
INSULT_RARE_REPLY = "լավ խելոք մնա հլը"

# Armenian weekdays -> Python weekday() (Mon=0 … Sun=6)
WEEKDAYS = {
    "erkushabti": 0, "երկուշաբթի": 0,
    "ereqshabti": 1, "երեքշաբթի": 1,
    "choreqshabti": 2, "չորեքշաբթի": 2,
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

def render(s: str, label: str, d: date, mode: str, self_user: bool = False) -> str:
    # self_user: SELF_USER_ID is asking about themselves -> "you" wording (ես)
    ds = d.strftime("%d.%m.%Y")
    if mode == "free":
        if s == OFF:
            return "հա ազատ ես ցավդ տանեմ" if self_user else "հա ազատ ա ցավդ տանեմ"
        return "չէ զբաղված ես ախպերս, ներող" if self_user else "չէ զբաղված ա ախպերս"
    # work mode
    if s == DAY:
        return "հա ցավդ տանեմ, ցերեկվա սմեն"
    if s == NIGHT:
        return "հա ընգերս, գիշերային պախատ"
    return "չէ բռատս, ազատ ես, գուլյատ արա" if self_user else "չէ բռատս, ազատ ա"

def answer(target: date, label: str, mode: str, self_user: bool = False) -> str:
    return render(shift_for(target), label, target, mode, self_user)

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
    "`/clip <url> <start> <end> [fade] [audio|video|both] [name]`\n\n"
    "• *start / end*: `M:SS`, `H:MM:SS` or seconds\n"
    "• *fade*: seconds of fade in/out, default " + f"{DEFAULT_FADE_SEC:g}" + "\n"
    "• *audio* = MP3 only (fastest), *video* = MP4 only, "
    "*both* = MP4 + MP3 (default)\n"
    "• *name*: file name for the result (spaces allowed), "
    "default is the video title\n\n"
    "Examples:\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51`\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51 audio`\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51 video Faint intro`\n"
    "`/clip https://youtu.be/LYU-8IFcDPw 0:31 0:51 1 audio Faint intro`\n\n"
    "Old style also works: `/clip <url> 0 31 0 51 ...`"
)

# words accepted for the output type (any case)
MODE_WORDS = {
    "audio": "audio", "mp3": "audio", "a": "audio", "sound": "audio", "աուդիո": "audio",
    "video": "video", "mp4": "video", "v": "video", "վիդեո": "video",
    "both": "both", "all": "both", "երկուսը": "both",
}

MODE_LABEL = {"audio": "MP3", "video": "MP4", "both": "MP4 + MP3"}


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


def _is_number(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?", token))


def parse_clip_args(args: list[str]) -> tuple[str, int, int, float, str, str | None]:
    """
    After /clip:
        <url> <start> <end> [fade] [audio|video|both] [name ...]
        <url> <s_min> <s_sec> <e_min> <e_sec> [fade] [mode] [name ...]   (old style)
    fade and mode may come in either order; everything after them is the name.
    Returns (url, start_sec, end_sec, fade, mode, name). Raises ValueError.
    """
    if len(args) < 3:
        raise ValueError("Not enough arguments.")
    url = args[0]
    rest = args[1:]

    # old 4-number style: four plain integers, seconds 0..59
    if (len(rest) >= 4 and all(re.fullmatch(r"\d+", t) for t in rest[:4])
            and int(rest[1]) < 60 and int(rest[3]) < 60):
        start = int(rest[0]) * 60 + int(rest[1])
        end = int(rest[2]) * 60 + int(rest[3])
        extra = rest[4:]
    else:
        start = parse_timecode(rest[0])
        end = parse_timecode(rest[1])
        extra = rest[2:]

    fade: float | None = None
    mode: str | None = None
    while extra:
        tok = extra[0]
        if fade is None and _is_number(tok):
            fade = float(tok)
        elif mode is None and tok.lower() in MODE_WORDS:
            mode = MODE_WORDS[tok.lower()]
        else:
            break
        extra = extra[1:]
    name = " ".join(extra).strip() or None

    if fade is None:
        fade = DEFAULT_FADE_SEC
    if fade > 30:
        raise ValueError(f"fade {fade:g}s is too long (max 30s)")
    if end <= start:
        raise ValueError("End time must be after start time.")
    return url, start, end, fade, mode or "both", name


async def _send_video(update: Update, path: Path, caption: str) -> bool:
    """Send the MP4 if it fits Telegram's bot limit. Returns True if sent."""
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > TELEGRAM_MAX_MB:
        await update.message.reply_text(
            f"⚠️ The MP4 is {size_mb:.0f} MB, above Telegram's {TELEGRAM_MAX_MB} MB "
            "bot limit, so I can't send it. Try a shorter range, or `audio`.",
            parse_mode="Markdown",
        )
        return False
    with open(path, "rb") as fh:
        await update.message.reply_document(
            document=fh, filename=path.name, caption=f"🎬 {caption}",
            read_timeout=600, write_timeout=600, connect_timeout=60,
        )
    return True


async def _send_audio(update: Update, path: Path, title: str) -> None:
    with open(path, "rb") as fh:
        await update.message.reply_audio(
            audio=fh, filename=path.name, title=title,
            read_timeout=600, write_timeout=600, connect_timeout=60,
        )


async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    try:
        url, start, end, fade, mode, name = parse_clip_args(context.args)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {e}\n\n{CLIP_USAGE}", parse_mode="Markdown"
        )
        return

    length = end - start
    status = await update.message.reply_text(
        f"⏳ {MODE_LABEL[mode]} · {length}s clip"
        + (f" · \"{name}\"" if name else "")
        + " · resolving the video…"
    )
    workdir = Path(tempfile.mkdtemp(prefix="berimor_clip_"))

    async with JOB_SEMAPHORE:
        try:
            # 1) Download + cut (+ encode). Blocking work runs in a thread.
            if mode == "audio":
                await status.edit_text("🎧 Downloading the audio and making the MP3…")
            else:
                await status.edit_text("⬇️ Downloading & encoding 1080p video… this can take a bit.")
            result = await asyncio.to_thread(
                download_clip, url, start, end, workdir,
                fade=fade, mode=mode, name=name,
            )
            label = name or result.title

            if mode == "audio":
                await status.edit_text("📤 Uploading the MP3…")
                await _send_audio(update, result.mp3_path, label)
                await status.delete()
                return

            # Warn if YouTube only served a sub-1080p source
            if result.height and result.height < 1080:
                await update.message.reply_text(
                    f"ℹ️ YouTube only served {result.width}x{result.height} for this "
                    "video, so the MP4 is padded to 1080p rather than truly 1080p."
                )

            # 2) MP3 from the finished MP4 (only for "both")
            mp3_path = None
            if mode == "both":
                await status.edit_text("🎧 Converting to MP3…")
                mp3_path = await asyncio.to_thread(convert_to_mp3, result.mp4_path)

            # 3) Upload what was asked for
            await status.edit_text("📤 Uploading…")
            await _send_video(update, result.mp4_path, label)
            if mp3_path:
                await _send_audio(update, mp3_path, label)

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
    # SELF_USER_ID may also use a "self" word (es / ես) instead of the name.
    user = update.effective_user
    is_self_user = bool(user and user.id == SELF_USER_ID)
    has_name = bool(NAME_RE.search(text)) or (is_self_user and bool(SELF_RE.search(text)))
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
            context.chat_data["awaiting_self"] = is_self_user
            await update.message.reply_text("ե՞րբ ցավդ տանեմ")
            return
        # date given: answer and stop waiting
        context.chat_data.pop("awaiting_since", None)
        context.chat_data.pop("awaiting_mode", None)
        context.chat_data.pop("awaiting_self", None)
        target, label = result
        await update.message.reply_text(answer(target, label, mode, is_self_user))
        return

    # not a full question: maybe it's the answer to a previous "erb?"
    awaiting = context.chat_data.get("awaiting_since")
    if awaiting and datetime.now(TZ) - awaiting <= PENDING_TTL:
        result = resolve_date(text, today)
        if result is not None:
            saved_mode = context.chat_data.get("awaiting_mode", "work")
            # use the wording for whoever asked the original question
            saved_self = context.chat_data.get("awaiting_self", False)
            context.chat_data.pop("awaiting_since", None)
            context.chat_data.pop("awaiting_mode", None)
            context.chat_data.pop("awaiting_self", None)
            target, label = result
            await update.message.reply_text(answer(target, label, saved_mode, saved_self))
            return

    # cinema words -> "kinaciatr", quoting the exact keyword that triggered it
    if KINO_RE.search(text):
        rp = quote_params(update.message, KINO_RE)
        try:
            await update.message.reply_text(KINO_REPLY, reply_parameters=rp)
        except Exception:
            await update.message.reply_text(KINO_REPLY)
        return

    # insult -> reply, quoting the exact keyword that triggered it
    # (1/100 rare reply, otherwise 1/7 each)
    # Only when the insult is aimed at the bot: the message must ALSO contain
    # a berimor word / @tag (SUMMON_RE). A bare insult in normal chat is ignored.
    if INSULT_RE.search(text) and SUMMON_RE.search(text):
        if random.random() < RARE_CHANCE:
            reply = INSULT_RARE_REPLY
        else:
            reply = random.choice(INSULT_REPLIES)
        rp = quote_params(update.message, INSULT_RE)
        try:
            await update.message.reply_text(reply, reply_parameters=rp)
        except Exception:
            # if the partial-quote is rejected, fall back to a normal reply
            await update.message.reply_text(reply)
        return

    # summon (word or @tag) -> random acknowledgement
    # (1/100 rare reply, otherwise 1/3 each)
    if SUMMON_RE.search(text):
        if random.random() < RARE_CHANCE:
            await update.message.reply_text(SUMMON_RARE_REPLY)
        else:
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
