"""
GymBot — No Excuses Edition
Telegram group workout tracker with Google Sheets backend.

Dependencies:
    pip install python-telegram-bot gspread google-auth apscheduler

Environment variables:
    BOT_TOKEN       — from BotFather
    GROUP_CHAT_ID   — your group's chat ID (negative number)
    SHEET_ID        — Google Sheet ID from the URL
"""

import os
import logging
import random
import calendar
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN     = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])
SHEET_ID      = os.environ["SHEET_ID"]
SESSIONS_GOAL = 3  # mandatory sessions per week

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_spreadsheet():
    creds  = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def tab(spreadsheet, name: str, headers: list):
    """Get or create a worksheet tab."""
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(name, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws


def sessions_tab(sp):
    return tab(sp, "Sessions", ["Name", "Logged On", "Workout Date", "Day", "Week", "Carry-in", "Type"])


def summary_tab(sp):
    return tab(sp, "Weekly Summary", ["Name", "Week", "Month", "Sessions", "Carry-in", "Target", "Skip Used", "Plank Owed"])


def skips_tab(sp):
    return tab(sp, "Skips", ["Name", "Month", "Used"])


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

DAY_ALIASES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
    "yesterday": -1,
    "today": 0,
}


def week_num(date: datetime) -> int:
    return date.isocalendar()[1]


def resolve_date(arg: str | None) -> datetime | None:
    """
    Resolve /workout [arg] to an actual date.
    Returns None if the resolved date is in a different week (cross-week backfill blocked).
    """
    today = datetime.now()
    if not arg:
        return today

    key = arg.lower().strip()

    if key == "yesterday":
        candidate = today - timedelta(days=1)
    elif key == "today":
        candidate = today
    elif key in DAY_ALIASES:
        target_wd  = DAY_ALIASES[key]
        current_wd = today.weekday()
        days_back  = (current_wd - target_wd) % 7
        candidate  = today if days_back == 0 else today - timedelta(days=days_back)
    else:
        return None  # unrecognised arg

    # Block cross-week backfill
    if week_num(candidate) != week_num(today):
        return None

    return candidate


def sessions_this_week(sessions_ws, name: str, week: int) -> list:
    return [
        r for r in sessions_ws.get_all_records()
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week
    ]


def carry_in_for(summary_ws, name: str, week: int) -> int:
    for r in summary_ws.get_all_records():
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week:
            return int(r.get("Carry-in", 0))
    return 0


def skip_used_this_month(skips_ws, name: str, month: int) -> bool:
    for r in skips_ws.get_all_records():
        if r["Name"].lower() == name.lower() and int(r["Month"]) == month:
            return str(r.get("Used", "")).lower() in ("true", "1", "yes")
    return False


def mark_skip(skips_ws, name: str, month: int):
    records = skips_ws.get_all_records()
    for i, r in enumerate(records):
        if r["Name"].lower() == name.lower() and int(r["Month"]) == month:
            skips_ws.update_cell(i + 2, 3, "TRUE")
            return
    skips_ws.append_row([name, month, "TRUE"])


def mark_plank_cleared(summary_ws, name: str, week: int):
    records = summary_ws.get_all_records()
    for i, r in enumerate(records):
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week:
            summary_ws.update_cell(i + 2, 8, "FALSE")  # col 8 = Plank Owed
            return


def planks_owed_this_week(summary_ws, week: int) -> list[str]:
    return [
        r["Name"] for r in summary_ws.get_all_records()
        if int(r["Week"]) == week and str(r.get("Plank Owed", "")).lower() == "true"
    ]


# ---------------------------------------------------------------------------
# MESSAGES
# ---------------------------------------------------------------------------

def pick(pool: list, **kw) -> str:
    return random.choice(pool).format(**kw)


HYPE = [
    "🔥 {name} is BUILT DIFFERENT. Session locked in.",
    "💪 {name} shows up again. No days off.",
    "👀 {name} in the building. Session counted. Let's go.",
    "🏋️ {name} doing what needs to be done. ✅",
    "{name} said let's GO. Logged. Keep it up.",
]

HYPE_BACKFILL = [
    "⏪ Time traveler {name} logs a {day} session. It counts!",
    "🕰️ {name} remembered {day}. Better late than never. Saved!",
    "📅 {name} backdating like a pro. {day} session locked in.",
]

DONE_WEEK = [
    "\n\n✅ That's {count}/{target} this week. You're DONE. Go rest 🛌",
    "\n\n🏁 {count}/{target} — week complete! Touch some grass.",
    "\n\n🎉 {count}/{target} this week. Mandatory suffering: complete.",
]

ABOVE_TARGET = "\n\n🏅 {count}/{target} — ABOVE the minimum. Iron energy."

PROGRESS = "\n\n📊 {count}/{target} this week. {left} more to go."

SKIP_MSG = [
    "⏭️ {name} burns the monthly lifeline. +1 session moves to next week. Use it wisely.",
    "🎟️ {name} cashed in their one skip for the month. Next week target: {next_target}.",
    "😮‍💨 {name} invokes the skip. That's your ONE this month. Don't waste it.",
]

PLANK_CONFIRMED = [
    "🪵 {name} survived the plank. Debt cleared. Don't let it happen again.",
    "😤 {name} did the plank. We respect the accountability. Don't repeat it.",
    "✅ {name}'s plank logged. Pain acknowledged. We move. 🫡",
]

WEEKLY_RECAP_HEADER = "📊 Week {week} recap — {date}\n"

MONTHLY_FOOTER = "\nSee you next month. No mercy. 💀"

GUIDEME_TEXT = """
👋 Welcome to GymBot — No Excuses Edition!

━━━━━━━━━━━━━━━━━━━━
🏋️ THE GOAL
━━━━━━━━━━━━━━━━━━━━
Do at least 3 workouts per week. Every week.

━━━━━━━━━━━━━━━━━━━━
📋 COMMANDS
━━━━━━━━━━━━━━━━━━━━
/workout — log today's session
/workout monday — backfill a day from this week
/workout yesterday — same, for yesterday
/skip — use your one monthly lifeline
/stats — see current week standings
/plank — confirm you did your punishment plank
/shame — see who owes a plank 😈
/monthly — trigger the monthly hall of fame

━━━━━━━━━━━━━━━━━━━━
⚠️ THE RULES
━━━━━━━━━━━━━━━━━━━━
• 3 sessions per week is the minimum
• Miss the week → 1 min plank, post video proof here
• 1 skip per month: carries exactly 1 session to next week (target becomes 4)
• No skip + missed → plank AND public shame
• Backfill works within the same week only

━━━━━━━━━━━━━━━━━━━━
📅 BOT SCHEDULE
━━━━━━━━━━━━━━━━━━━━
• Sunday 9pm → weekly recap + roast
• Monday 9am → plank debt reminder
• Last day of month → Hall of Fame

━━━━━━━━━━━━━━━━━━━━
🏅 MONTHLY BADGES
━━━━━━━━━━━━━━━━━━━━
🏅 Iron — went above 3 in at least one week
🥈 Silver — hit exactly 3/3 every week
🥉 Bronze — showed up most weeks
🪵 Plank King — most planks (not a compliment)
📈 Glow Up — most improved vs last month

Good luck. You'll need it. 💀
""".strip()

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name  = update.effective_user.first_name
    arg   = context.args[0] if context.args else None
    today = datetime.now()
    date  = resolve_date(arg)

    if date is None:
        if arg:
            await update.message.reply_text(
                f"❌ {name}, can't backfill across weeks — Sunday is the cutoff. "
                "You can only log days within the current week.\n"
                "Try: /workout monday, /workout friday, /workout yesterday"
            )
        else:
            await update.message.reply_text(f"❓ {name}, unrecognised day. Try /workout monday or /workout yesterday.")
        return

    sp          = get_spreadsheet()
    sess_ws     = sessions_tab(sp)
    summ_ws     = summary_tab(sp)
    week        = week_num(date)
    existing    = sessions_this_week(sess_ws, name, week)
    workout_str = date.strftime("%Y-%m-%d")
    day_name    = date.strftime("%A")

    # Duplicate guard
    if any(r["Workout Date"] == workout_str for r in existing):
        await update.message.reply_text(f"🤔 {name}, already logged a session for {day_name}. One per day!")
        return

    carry   = carry_in_for(summ_ws, name, week)
    target  = SESSIONS_GOAL + carry
    is_back = date.date() != today.date()

    sess_ws.append_row([
        name,
        today.strftime("%Y-%m-%d"),
        workout_str,
        day_name,
        week,
        carry,
        "backfill" if is_back else "workout",
    ])

    count = len(existing) + 1

    msg = pick(HYPE_BACKFILL, name=name, day=day_name) if is_back else pick(HYPE, name=name)

    if count >= target:
        if count > target:
            msg += ABOVE_TARGET.format(count=count, target=target)
        else:
            msg += pick(DONE_WEEK, count=count, target=target)
    else:
        msg += PROGRESS.format(count=count, target=target, left=target - count)

    await update.message.reply_text(msg)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name  = update.effective_user.first_name
    today = datetime.now()
    month = today.month
    sp    = get_spreadsheet()
    sk_ws = skips_tab(sp)

    if skip_used_this_month(sk_ws, name, month):
        await update.message.reply_text(
            f"❌ {name} already used their skip this month. No more lifelines.\n"
            "Get those sessions in or face the plank. 🪵"
        )
        return

    mark_skip(sk_ws, name, month)

    # Register carry-in for next week
    summ_ws   = summary_tab(sp)
    next_week = week_num(today) + 1
    rows      = summ_ws.get_all_records()
    found     = False
    for i, r in enumerate(rows):
        if r["Name"].lower() == name.lower() and int(r["Week"]) == next_week:
            new_carry = int(r.get("Carry-in", 0)) + 1
            summ_ws.update_cell(i + 2, 5, new_carry)
            summ_ws.update_cell(i + 2, 6, SESSIONS_GOAL + new_carry)
            found = True
            break
    if not found:
        summ_ws.append_row([name, next_week, month, 0, 1, SESSIONS_GOAL + 1, "FALSE", "FALSE"])

    await update.message.reply_text(pick(SKIP_MSG, name=name, next_target=SESSIONS_GOAL + 1))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today   = datetime.now()
    week    = week_num(today)
    sp      = get_spreadsheet()
    sess_ws = sessions_tab(sp)
    summ_ws = summary_tab(sp)

    all_sessions  = sess_ws.get_all_records()
    week_sessions = [r for r in all_sessions if int(r["Week"]) == week]
    names         = sorted({r["Name"] for r in week_sessions})

    if not names:
        await update.message.reply_text("📊 No sessions logged this week yet. Who's going first? 👀")
        return

    lines = [f"📊 Week {week} — current standings\n"]
    for name in names:
        count  = len([r for r in week_sessions if r["Name"].lower() == name.lower()])
        carry  = carry_in_for(summ_ws, name, week)
        target = SESSIONS_GOAL + carry
        carry_str = f" (+{carry} carry-in)" if carry else ""

        if count >= target:
            icon = "🏅" if count > target else "✅"
        elif count > 0:
            icon = "⚠️"
        else:
            icon = "❌"

        lines.append(f"{icon} {name}: {count}/{target}{carry_str}")

    await update.message.reply_text("\n".join(lines))


async def cmd_plank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name    = update.effective_user.first_name
    today   = datetime.now()
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)

    rows = summ_ws.get_all_records()
    for i, r in enumerate(rows):
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week:
            if str(r.get("Plank Owed", "")).lower() == "true":
                mark_plank_cleared(summ_ws, name, week)
                await update.message.reply_text(pick(PLANK_CONFIRMED, name=name))
            else:
                await update.message.reply_text(f"🤔 {name}, you don't owe a plank. Lucky you.")
            return

    await update.message.reply_text(f"🤔 {name}, you don't owe a plank. Lucky you.")


async def cmd_shame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today   = datetime.now()
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    owing   = planks_owed_this_week(summ_ws, week)

    if not owing:
        await update.message.reply_text("✅ Nobody owes a plank right now. Suspiciously wholesome.")
        return

    names_str = ", ".join(owing)
    await update.message.reply_text(
        f"🪵 PLANK DEBTORS this week:\n\n{names_str}\n\n"
        "Post your 1-minute video and use /plank to clear it. The group is watching. 👀"
    )


async def cmd_guideme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GUIDEME_TEXT)


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _post_monthly_review(context.bot)


# ---------------------------------------------------------------------------
# SCHEDULED JOBS
# ---------------------------------------------------------------------------

async def _post_weekly_summary(bot):
    today   = datetime.now()
    week    = week_num(today)
    sp      = get_spreadsheet()
    sess_ws = sessions_tab(sp)
    summ_ws = summary_tab(sp)
    sk_ws   = skips_tab(sp)
    month   = today.month

    all_sessions  = sess_ws.get_all_records()
    week_sessions = [r for r in all_sessions if int(r["Week"]) == week]
    names         = sorted({r["Name"] for r in week_sessions})

    if not names:
        return

    lines         = [WEEKLY_RECAP_HEADER.format(week=week, date=today.strftime("%b %d"))]
    planks_owed   = []
    skips_used    = []
    all_skips_info = []

    for name in names:
        count  = len([r for r in week_sessions if r["Name"].lower() == name.lower()])
        carry  = carry_in_for(summ_ws, name, week)
        target = SESSIONS_GOAL + carry
        used_skip = skip_used_this_month(sk_ws, name, month)
        carry_str = f" (+{carry} carry)" if carry else ""

        if count >= target:
            extra = count - target
            badge = " 🏅" if extra > 0 else ""
            lines.append(f"✅ {name}: {count}/{target}{carry_str}{badge}")
            plank = False
            skip_flag = False
        elif used_skip:
            lines.append(f"⏭️ {name}: {count}/{target}{carry_str} (skip used — +1 next week)")
            skips_used.append(name)
            plank = False
            skip_flag = True
        else:
            lines.append(f"❌ {name}: {count}/{target}{carry_str} — PLANK TIME 🪵")
            planks_owed.append(name)
            plank = True
            skip_flag = False

        # Write to summary tab
        summ_ws.append_row([name, week, month, count, carry, target, str(skip_flag).upper(), str(plank).upper()])
        all_skips_info.append(f"{name} {'0' if used_skip else '1'}")

    if planks_owed:
        lines.append(f"\n🪵 Plank owed: {', '.join(planks_owed)}")
        lines.append("1 min plank. Post a video. Then use /plank to clear it.")

    lines.append(f"\nSkips left this month: {' · '.join(all_skips_info)}")

    await bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))


async def _post_plank_reminder(bot):
    today   = datetime.now()
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    owing   = planks_owed_this_week(summ_ws, week)

    if not owing:
        return

    names_str = ", ".join(owing)
    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=(
            f"🪵 Good morning! Plank reminder for: {names_str}\n\n"
            "Post your 1-minute video and use /plank to clear the debt. "
            "Don't make us ask again. 😤"
        ),
    )


async def _post_monthly_review(bot):
    today   = datetime.now()
    month   = today.month
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    sk_ws   = skips_tab(sp)

    all_rows    = summ_ws.get_all_records()
    month_rows  = [r for r in all_rows if int(r.get("Month", 0)) == month]

    if not month_rows:
        return

    names  = sorted({r["Name"] for r in month_rows})
    stats  = {}

    for name in names:
        rows     = [r for r in month_rows if r["Name"].lower() == name.lower()]
        sessions = sum(int(r.get("Sessions", 0)) for r in rows)
        target   = sum(int(r.get("Target", SESSIONS_GOAL)) for r in rows)
        planks   = sum(1 for r in rows if str(r.get("Plank Owed", "")).lower() == "true")
        above3   = sum(1 for r in rows if int(r.get("Sessions", 0)) > SESSIONS_GOAL)
        stats[name] = dict(sessions=sessions, target=target, planks=planks, above3=above3)

    sorted_names = sorted(names, key=lambda n: stats[n]["sessions"], reverse=True)
    month_name   = today.strftime("%B").upper()

    lines = [f"🏆 {month_name} RECAP — THE COUNCIL OF GAINS 🏆\n"]
    for name in sorted_names:
        s     = stats[name]
        badge = ""
        if s["above3"] > 0:
            badge = " 🏅 Iron"
        elif s["planks"] == 0 and s["sessions"] >= s["target"]:
            badge = " 🥈 Silver"
        elif s["sessions"] >= s["target"] * 0.7:
            badge = " 🥉 Bronze"
        skip_used = skip_used_this_month(sk_ws, name, month)
        skip_str  = " · skip used" if skip_used else ""
        lines.append(f"{name}: {s['sessions']}/{s['target']} sessions · {s['planks']} planks{skip_str}{badge}")

    best      = max(names, key=lambda n: stats[n]["sessions"])
    plank_kng = max(names, key=lambda n: stats[n]["planks"])
    lines.append(f"\n🥇 Most sessions: {best}")
    if stats[plank_kng]["planks"] > 0:
        lines.append(f"🪵 Plank King: {plank_kng} ({stats[plank_kng]['planks']} planks)")

    # Most improved: needs last month data — skip if not available
    lines.append(MONTHLY_FOOTER)

    await bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))

    # Reset skip records for the new month
    sk_ws.clear()
    sk_ws.append_row(["Name", "Month", "Used"])
    log.info("Skips reset for new month.")


def is_last_day_of_month() -> bool:
    today    = datetime.now()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


# Scheduler wrapper functions (APScheduler needs plain callables)
async def job_sunday_recap(bot):
    log.info("Running Sunday weekly recap")
    await _post_weekly_summary(bot)


async def job_monday_reminder(bot):
    log.info("Running Monday plank reminder")
    await _post_plank_reminder(bot)


async def job_month_end(bot):
    if is_last_day_of_month():
        log.info("Running month-end review")
        await _post_monthly_review(bot)

# Adding flask endpoint
from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route("/")
def home():
    return "alive"

def run_web():
    app.run(host="0.0.0.0", port=8080)

Thread(target=run_web).start()

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("workout", cmd_workout))
    app.add_handler(CommandHandler("skip",    cmd_skip))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("plank",   cmd_plank))
    app.add_handler(CommandHandler("shame",   cmd_shame))
    app.add_handler(CommandHandler("guideme", cmd_guideme))
    app.add_handler(CommandHandler("monthly", cmd_monthly))

    scheduler = AsyncIOScheduler(timezone="UTC")
    bot       = app.bot

    # Sunday 9pm UTC
    scheduler.add_job(job_sunday_recap,   "cron", day_of_week="sun", hour=21, minute=0,  args=[bot])
    # Monday 9am UTC
    scheduler.add_job(job_monday_reminder,"cron", day_of_week="mon", hour=9,  minute=0,  args=[bot])
    # Every day at 9pm — checks if it's the last day of the month
    scheduler.add_job(job_month_end,      "cron",                    hour=21, minute=30, args=[bot])

    scheduler.start()
    log.info("GymBot started. Let the suffering begin.")
    app.run_polling()


if __name__ == "__main__":
    main()