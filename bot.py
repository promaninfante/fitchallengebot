"""
GymBot — No Excuses Edition
Telegram group workout tracker with Google Sheets backend.

Dependencies:
    pip install python-telegram-bot gspread google-auth apscheduler flask

Environment variables:
    BOT_TOKEN       — from BotFather
    GROUP_CHAT_ID   — your group's chat ID (negative number)
    SHEET_ID        — Google Sheet ID from the URL
"""

import os
import json
import random
import logging
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Thread

import gspread
from flask import Flask
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
TZ            = ZoneInfo("America/New_York")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FLASK KEEP-ALIVE
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "alive"

_PORT = int(os.environ.get("PORT", 8080))
Thread(target=lambda: flask_app.run(host="0.0.0.0", port=_PORT), daemon=True).start()

# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_spreadsheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials-google.json", scopes=SCOPES)
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
    # NOTE: if the Sessions tab already exists without "Notes", add it manually as column H header
    return tab(sp, "Sessions", ["Name", "Logged On", "Workout Date", "Day", "Week", "Carry-in", "Type", "Notes"])


def summary_tab(sp):
    return tab(sp, "Weekly Summary", ["Name", "Week", "Month", "Sessions", "Carry-in", "Target", "Skip Used", "Plank Owed"])


def weekly_tracker_tab(sp):
    try:
        return sp.worksheet("Weekly Tracker")
    except gspread.WorksheetNotFound:
        return sp.add_worksheet("Weekly Tracker", rows=50, cols=20)


def skips_tab(sp):
    return tab(sp, "Skips", ["Name", "Month", "Used"])


def week_cell(sessions: int, target: int) -> str:
    if sessions == 0:
        return "0 ❌"
    elif sessions < target:
        return f"{sessions} ⚠️"
    elif sessions > target:
        return f"{sessions} 🏅"
    else:
        return f"{sessions} ✅"


def compute_badge(rows: list, target_per_week: int) -> str:
    if not rows:
        return ""
    above3 = sum(1 for r in rows if int(r.get("Sessions", 0)) > SESSIONS_GOAL)
    planks = sum(1 for r in rows if str(r.get("Plank Owed", "")).upper() == "TRUE")
    total  = sum(int(r.get("Sessions", 0)) for r in rows)
    weeks  = len(rows)
    if above3 > 0:
        return "🏅 Iron"
    elif planks == 0 and all(int(r.get("Sessions", 0)) >= int(r.get("Target", SESSIONS_GOAL)) for r in rows):
        return "🥈 Silver"
    elif total >= weeks * SESSIONS_GOAL * 0.7:
        return "🥉 Bronze"
    return ""


def rebuild_weekly_tracker(sp):
    """Rebuild the visual pivot tab: one row per person, one column per week."""
    sess_ws = sessions_tab(sp)
    summ_ws = summary_tab(sp)
    sk_ws   = skips_tab(sp)
    tracker = weekly_tracker_tab(sp)

    today      = datetime.now(TZ)
    month      = today.month
    all_sess   = sess_ws.get_all_records()
    all_summ   = summ_ws.get_all_records()
    all_skips  = sk_ws.get_all_records()

    month_sess = [r for r in all_sess if datetime.strptime(r["Workout Date"], "%Y-%m-%d").month == month]
    weeks      = sorted({int(r["Week"]) for r in month_sess})

    if not weeks:
        return

    names   = sorted({r["Name"] for r in month_sess})
    headers = ["Name"] + [f"Wk {w}" for w in weeks] + ["Total 💪", "Planks 🪵", "Skip ⏭️", "Badge"]
    rows    = [headers]

    for name in names:
        person_summ = [r for r in all_summ if r["Name"].lower() == name.lower() and int(r.get("Month", 0)) == month]
        skip_used   = any(
            r["Name"].lower() == name.lower() and int(r["Month"]) == month
            and str(r.get("Used", "")).upper() in ("TRUE", "1", "YES")
            for r in all_skips
        )
        planks = sum(
            1 for r in all_summ
            if r["Name"].lower() == name.lower()
            and str(r.get("Plank Owed", "")).upper() == "TRUE"
        )

        total    = 0
        wk_cells = []
        for w in weeks:
            w_sessions = [r for r in month_sess if r["Name"].lower() == name.lower() and int(r["Week"]) == w]
            count      = len(w_sessions)
            carry      = next((int(sr.get("Carry-in", 0)) for sr in all_summ
                               if sr["Name"].lower() == name.lower() and int(sr.get("Week", 0)) == w), 0)
            wk_cells.append(week_cell(count, SESSIONS_GOAL + carry))
            total += count

        badge = compute_badge(person_summ, SESSIONS_GOAL)
        rows.append([name] + wk_cells + [total, planks, "Yes" if skip_used else "No", badge])

    tracker.clear()
    tracker.update(rows, value_input_option="RAW")


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
    today = datetime.now(TZ)
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
        return None

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
            summary_ws.update_cell(i + 2, 8, "FALSE")
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
PROGRESS     = "\n\n📊 {count}/{target} this week. {left} more to go."

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
MONTHLY_FOOTER      = "\nSee you next month. No mercy. 💀"

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
/workout legs day 🦵 — log today with a description
/workout monday ran 5k — backfill a day with a note
/workout yesterday — backfill yesterday
/skip — use your one monthly lifeline
/stats — see current week standings
/weekly — trigger the weekly recap manually
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
🥈 Silver — hit exactly 3/3 every week, zero planks
🥉 Bronze — showed up most weeks
🪵 Plank King — most planks (not a compliment)
📈 Glow Up — most improved vs last month

Good luck. You'll need it. 💀
""".strip()

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

def upsert_weekly_summary(summ_ws, name: str, week: int, month: int, sessions: int, carry: int):
    target  = SESSIONS_GOAL + carry
    records = summ_ws.get_all_records()
    for i, r in enumerate(records):
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week:
            summ_ws.update_cell(i + 2, 4, sessions)
            summ_ws.update_cell(i + 2, 6, target)
            return
    summ_ws.append_row([name, week, month, sessions, carry, target, "FALSE", "FALSE"])


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name  = update.effective_user.first_name
    today = datetime.now(TZ)

    # If the first arg is a known day alias, use it as the date and the rest as a note.
    # Otherwise log for today and treat all args as the note.
    first_arg = context.args[0].lower().strip() if context.args else None
    if first_arg and first_arg in DAY_ALIASES:
        date  = resolve_date(first_arg)
        notes = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    else:
        date  = today
        notes = " ".join(context.args) if context.args else ""

    if date is None:
        await update.message.reply_text(
            f"❌ {name}, can't backfill across weeks — Sunday is the cutoff.\n"
            "Try: /workout monday, /workout friday, /workout yesterday"
        )
        return

    sp          = get_spreadsheet()
    sess_ws     = sessions_tab(sp)
    summ_ws     = summary_tab(sp)
    week        = week_num(date)
    month       = date.month
    existing    = sessions_this_week(sess_ws, name, week)
    workout_str = date.strftime("%Y-%m-%d")
    day_name    = date.strftime("%A")

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
        notes,
    ])

    count = len(existing) + 1
    upsert_weekly_summary(summ_ws, name, week, month, count, carry)
    rebuild_weekly_tracker(sp)

    msg = pick(HYPE_BACKFILL, name=name, day=day_name) if is_back else pick(HYPE, name=name)

    if count >= target:
        msg += ABOVE_TARGET.format(count=count, target=target) if count > target else pick(DONE_WEEK, count=count, target=target)
    else:
        msg += PROGRESS.format(count=count, target=target, left=target - count)

    await update.message.reply_text(msg)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name  = update.effective_user.first_name
    today = datetime.now(TZ)
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
    today   = datetime.now(TZ)
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
        count     = len([r for r in week_sessions if r["Name"].lower() == name.lower()])
        carry     = carry_in_for(summ_ws, name, week)
        target    = SESSIONS_GOAL + carry
        carry_str = f" (+{carry} carry-in)" if carry else ""
        icon      = "🏅" if count > target else "✅" if count >= target else "⚠️" if count > 0 else "❌"
        lines.append(f"{icon} {name}: {count}/{target}{carry_str}")

    await update.message.reply_text("\n".join(lines))


async def cmd_plank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name    = update.effective_user.first_name
    today   = datetime.now(TZ)
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)

    for i, r in enumerate(summ_ws.get_all_records()):
        if r["Name"].lower() == name.lower() and int(r["Week"]) == week:
            if str(r.get("Plank Owed", "")).lower() == "true":
                mark_plank_cleared(summ_ws, name, week)
                await update.message.reply_text(pick(PLANK_CONFIRMED, name=name))
            else:
                await update.message.reply_text(f"🤔 {name}, you don't owe a plank. Lucky you.")
            return

    await update.message.reply_text(f"🤔 {name}, you don't owe a plank. Lucky you.")


async def cmd_shame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today   = datetime.now(TZ)
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    owing   = planks_owed_this_week(summ_ws, week)

    if not owing:
        await update.message.reply_text("✅ Nobody owes a plank right now. Suspiciously wholesome.")
        return

    await update.message.reply_text(
        f"🪵 PLANK DEBTORS this week:\n\n{', '.join(owing)}\n\n"
        "Post your 1-minute video and use /plank to clear it. The group is watching. 👀"
    )


async def cmd_guideme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GUIDEME_TEXT)


# ---------------------------------------------------------------------------
# BUILD HELPERS — return text only, no sending
# Commands use reply_text (scoped to one message, never duplicated).
# Scheduler uses bot.send_message to broadcast.
# Keeping these separate is what prevents the double-message bug.
# ---------------------------------------------------------------------------

async def _build_weekly_summary() -> str | None:
    today   = datetime.now(TZ)
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
        return None

    lines          = [WEEKLY_RECAP_HEADER.format(week=week, date=today.strftime("%b %d"))]
    planks_owed    = []
    all_skips_info = []

    for name in names:
        count     = len([r for r in week_sessions if r["Name"].lower() == name.lower()])
        carry     = carry_in_for(summ_ws, name, week)
        target    = SESSIONS_GOAL + carry
        used_skip = skip_used_this_month(sk_ws, name, month)
        carry_str = f" (+{carry} carry)" if carry else ""

        if count >= target:
            badge = " 🏅" if count > target else ""
            lines.append(f"✅ {name}: {count}/{target}{carry_str}{badge}")
            plank     = False
            skip_flag = False
        elif used_skip:
            lines.append(f"⏭️ {name}: {count}/{target}{carry_str} (skip used — +1 next week)")
            plank     = False
            skip_flag = True
        else:
            lines.append(f"❌ {name}: {count}/{target}{carry_str} — PLANK TIME 🪵")
            planks_owed.append(name)
            plank     = True
            skip_flag = False

        summ_ws.append_row([name, week, month, count, carry, target, str(skip_flag).upper(), str(plank).upper()])
        all_skips_info.append(f"{name} {'0' if used_skip else '1'}")

    if planks_owed:
        lines.append(f"\n🪵 Plank owed: {', '.join(planks_owed)}")
        lines.append("1 min plank. Post a video. Then use /plank to clear it.")

    lines.append(f"\nSkips left this month: {' · '.join(all_skips_info)}")
    return "\n".join(lines)


async def _build_monthly_review() -> str | None:
    today   = datetime.now(TZ)
    month   = today.month
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    sk_ws   = skips_tab(sp)

    all_rows   = summ_ws.get_all_records()
    month_rows = [r for r in all_rows if int(r.get("Month", 0)) == month]

    if not month_rows:
        return None

    names = sorted({r["Name"] for r in month_rows})
    stats = {}

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
        s         = stats[name]
        badge     = " 🏅 Iron" if s["above3"] > 0 else " 🥈 Silver" if s["planks"] == 0 and s["sessions"] >= s["target"] else " 🥉 Bronze" if s["sessions"] >= s["target"] * 0.7 else ""
        skip_used = skip_used_this_month(sk_ws, name, month)
        skip_str  = " · skip used" if skip_used else ""
        lines.append(f"{name}: {s['sessions']}/{s['target']} sessions · {s['planks']} planks{skip_str}{badge}")

    best      = max(names, key=lambda n: stats[n]["sessions"])
    plank_kng = max(names, key=lambda n: stats[n]["planks"])
    lines.append(f"\n🥇 Most sessions: {best}")
    if stats[plank_kng]["planks"] > 0:
        lines.append(f"🪵 Plank King: {plank_kng} ({stats[plank_kng]['planks']} planks)")
    lines.append(MONTHLY_FOOTER)

    # Reset skips for the new month
    sk_ws.clear()
    sk_ws.append_row(["Name", "Month", "Used"])
    log.info("Skips reset for new month.")

    return "\n".join(lines)


# Commands — reply_text is scoped to the message, never sent twice
async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_weekly_summary()
    await update.message.reply_text(text or "📊 No sessions logged this week yet.")


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_monthly_review()
    await update.message.reply_text(text or "🏆 No data for this month yet.")


# ---------------------------------------------------------------------------
# SCHEDULED JOBS — broadcast to group via bot.send_message
# ---------------------------------------------------------------------------

async def _post_weekly_summary(bot):
    text = await _build_weekly_summary()
    if text:
        await bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


async def _post_plank_reminder(bot):
    today   = datetime.now(TZ)
    week    = week_num(today)
    sp      = get_spreadsheet()
    summ_ws = summary_tab(sp)
    owing   = planks_owed_this_week(summ_ws, week)

    if not owing:
        return

    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=(
            f"🪵 Good morning! Plank reminder for: {', '.join(owing)}\n\n"
            "Post your 1-minute video and use /plank to clear the debt. "
            "Don't make us ask again. 😤"
        ),
    )


async def _post_monthly_review(bot):
    text = await _build_monthly_review()
    if text:
        await bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


def is_last_day_of_month() -> bool:
    today    = datetime.now(TZ)
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


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
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))

    scheduler = AsyncIOScheduler(timezone="America/New_York")
    bot       = app.bot

    scheduler.add_job(job_sunday_recap,    "cron", day_of_week="sun", hour=23, minute=59, args=[bot])
    scheduler.add_job(job_monday_reminder, "cron", day_of_week="mon", hour=9,  minute=0,  args=[bot])
    scheduler.add_job(job_month_end,       "cron",                    hour=23, minute=59, args=[bot])

    scheduler.start()
    for job in scheduler.get_jobs():
        log.info(f"{job.id} next run: {job.next_run_time}")
    log.info("GymBot started. Let the suffering begin.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()