import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Config
BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Asia/Yerevan")  # Armenia time (UTC+4)
PERSON = "Kre"                # display name in replies

# How long the bot keeps waiting for a date after asking "erb?"
PENDING_TTL = timedelta(minutes=10)

# Anchor = a known DAY-shift date. Cycle is [Day, Night, Off, Off].
ANCHOR = date(2026, 8, 4)

DAY, NIGHT, OFF = "day", "night", "off"

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
# work words: gorc(i), ashxat(anq/el/um), smen, airport...
WORK_RE = re.compile(r"(gorc|gordz|gorts|ashxat|ashkhat|smen|airport|aeroport|աերոպորտ|օդանավ|գորձ|գործ|աշխատ|սմեն)", re.IGNORECASE)
# free word: azat / ազատ
AZAT_RE = re.compile(r"(azat|ազատ)", re.IGNORECASE)

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

    # today (esor / aysor / էսօր / այսօր)
    if re.search(r"(esor|aysor|hima|hmi|էսօր|այսօր|հիմա|հմի|)", text):
        return today, "esor"
    # tomorrow (vaghy / vagy / vaxy / վաղը)
    if re.search(r"(vag|vax|վաղ)", text):
        return today + timedelta(days=1), "vaghy"
    # yesterday (erek / էrek / երեկ / էրեկ)
    if re.search(r"(erek|ereg|երեկ|էրեկ|երեգ|էրէգ|)", text):
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

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("Bot runnin'")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
