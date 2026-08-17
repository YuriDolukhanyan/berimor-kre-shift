import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Config
BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Asia/Yerevan") # Armenia time (UTC+4)
PERSON = "Kren" # display name in replies

# How long the bot keeps waiting for a date after asking "erb?"
PENDING_TTL = timedelta(minutes=10)

# Anchor = a known DAY-shift date. Cycle is [Day, Night, Off, Off].
ANCHOR = date(2026, 8, 4)

DAY, NIGHT, OFF = "day", "night", "off"

def shift_for(d: date) -> str:
    pos = (d - ANCHOR).days % 4 # Python % is always >= 0
    if pos == 0:
        return DAY
    if pos == 1:
        return NIGHT
    return OFF

# Keyword matching
# name roots (kre/kren/krem/kremush/kremy, kar/karo/karapet, atabek/atabekyan)
NAME_RE = re.compile(r"\b(kre|kar|atabek|կրե|կար|աթաբեկ)\w*", re.IGNORECASE)
# work words: gorc(i), ashxat(anq/el/um)
WORK_RE = re.compile(r"(gorc|ashxat|smen|airport|aeroport|գործ|աշխատ|սմեն)", re.IGNORECASE)

# Armenian weekdays -> Python weekday() (Mon=0 Sun=6)
WEEKDAYS = {
    "erkushabti": 0,
    "ereqshabti": 1,
    "chorekshabti": 2,
    "hingshabti": 3,
    "urbat": 4,
    "shabat": 5,
    "kiraki": 6,
}

def next_weekday(today: date, wd: int) -> date:
    diff = (wd - today.weekday()) % 7 # 0 == today
    return today + timedelta(days=diff)

def resolve_date(text: str, today: date):
    # "amsi 12" -> day 12 of current month
    m = re.search(r"amsi\s+(\d{1,2})", text)
    if m:
        day = int(m.group(1))
        try:
            return today.replace(day=day), f"amsi {day}-in"
        except ValueError:
            return None # bad date -> ask when
    # weekday name -> nearest upcoming occurrence (incl. today)
    for key, wd in WEEKDAYS.items():
        if re.search(rf"\b{key}", text):
            return next_weekday(today, wd), key
    if re.search(r"\besor", text): # only "today" if said explicitly
        return today, "esor"
    if re.search(r"\bvag", text): # vaghy / vagy / vaxy = tomorrow
        return today + timedelta(days=1), "vaghy"
    if re.search(r"\berek", text): # erek = yesterday
        return today - timedelta(days=1), "erek"
    return None # no time word found

def render(s: str, label: str, d: date) -> str:
    ds = d.strftime("%d.%m.%Y")
    if s == DAY:
        return f"հա ցավդ տանեմ, գործի ա ցերեկվա սմեն"
    if s == NIGHT:
        return f"հա ընգերս, գիշերային պախատ"
    return f"չէ բռատս, ազատ ա"

def answer(target: date, label: str) -> str:
    return render(shift_for(target), label, target)

# Handler
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.lower()
    today = datetime.now(TZ).date()

    # A real question needs BOTH a name and a work word.
    is_question = bool(NAME_RE.search(text) and WORK_RE.search(text))

    if is_question:
        result = resolve_date(text, today)
        if result is None:
            # no date given: ask "when?" and start waiting
            context.chat_data["awaiting_since"] = datetime.now(TZ)
            await update.message.reply_text("ե՞rb ցավդ տանեմ")
            return
        # date given: answer and stop waiting
        context.chat_data.pop("awaiting_since", None)
        target, label = result
        await update.message.reply_text(answer(target, label))
        return

    awaiting = context.chat_data.get("awaiting_since")
    if awaiting and datetime.now(TZ) - awaiting <= PENDING_TTL:
        result = resolve_date(text, today)
        if result is not None:
            context.chat_data.pop("awaiting_since", None)
            target, label = result
            await update.message.reply_text(answer(target, label))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("Bot runnin'")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
