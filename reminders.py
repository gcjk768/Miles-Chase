"""Expiry reminders: a Telegram message 60, 30 and 7 days before points or miles expire.

Reads the `exp YYYY-MM` dates in data/my_points.txt and counts each one as expiring at the end
of that month. Runs without Claude. Each reminder is sent once; state/reminders_sent.json
remembers which ones went out.
"""

import calendar
import json
import re
from datetime import date

import run_miles
import vault

THRESHOLDS = (7, 30, 60)  # days before expiry
SENT = run_miles.STATE / "reminders_sent.json"
EXPIRY = re.compile(r"\bexp\s+(\d{4})-(\d{2})\b", re.IGNORECASE)
NAMES = {"CR": "Citi Rewards", "CPM": "Citi PremierMiles", "SCR": "SC Rewards", "KF": "KrisFlyer"}
WHAT_TO_DO = {
    "CR": "Search Saver seats on singaporeair.com, then transfer in the Citi Mobile app. "
          "Allow 1 to 3 working days.",
    "CPM": "Search Saver seats on singaporeair.com, then transfer in the Citi Mobile app. "
           "Allow 1 to 3 working days.",
    "SCR": "Search Saver seats on singaporeair.com, then transfer through the 360° Rewards portal. "
           "Allow 1 to 3 working days.",
    "KF": "Book an award with them before then.",
}


def expiries(lines):
    """(card, points, last day of the expiry month) for each balance line with an expiry."""
    found = []
    for line in lines:
        balance = run_miles.BALANCE_LINE.match(line)
        expiry = EXPIRY.search(line)
        if not (balance and expiry) or balance.group(1).upper() not in run_miles.CONVERSIONS:
            continue
        year, month = int(expiry.group(1)), int(expiry.group(2))
        if not 1 <= month <= 12:
            continue
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        found.append((balance.group(1).upper(), int(balance.group(2).replace(",", "")), last_day))
    return found


def due(today, lines, sent):
    """Reminders to send now, as (keys to mark as sent, message) pairs."""
    reminders = []
    for card, points, expires in expiries(lines):
        days = (expires - today).days
        if days < 0 or points <= 0:
            continue
        crossed = [t for t in THRESHOLDS if days <= t]
        if not crossed:
            continue
        # Only the closest threshold is sent. The wider ones are marked too, so a balance
        # first seen at 20 days out gets one reminder, not three.
        keys = [f"{card}:{expires.isoformat()}:{t}" for t in crossed]
        if keys[0] in sent:
            continue
        reminders.append((keys, message(card, points, expires, days)))
    return reminders


def message(card, points, expires, days):
    """The reminder as a Telegram HTML card."""
    left = f"{days} day{'s' if days != 1 else ''} left"
    lines = [f"💳 <b>{NAMES[card]}</b> · expires end {expires:%b %Y}"]
    if card == "KF":
        lines.append(f"💰 {points:,} miles expire")
    else:
        miles = run_miles.card_miles(card, points)
        fee = run_miles.transfer_fee(card, points)
        about = "about " if card in run_miles.APPROX_FEES else ""
        lines += [f"💰 {points:,} {run_miles.UNITS[card]} ({miles:,} KrisFlyer miles)",
                  f"💵 Transfer fee {about}S${fee:,.2f}"]
    return run_miles.card("reminder", left, "\n".join(lines),
                          f"💡 <i>{run_miles.esc(WHAT_TO_DO[card])}</i>")


def load_sent():
    try:
        return set(json.loads(SENT.read_text())) if SENT.exists() else set()
    except (ValueError, OSError, TypeError):
        return set()  # unreadable file: start again rather than stop reminders


def run(today, send):
    """Send due reminders with `send(text)`, then remember them. Returns how many were sent."""
    sent = load_sent()
    lines = run_miles.read_lines(run_miles.MY_POINTS)
    balances = run_miles.parse_balances(lines)
    reminders = due(today, lines, sent)
    for keys, text in reminders:
        send(text)
        sent.update(keys)
        SENT.parent.mkdir(parents=True, exist_ok=True)
        SENT.write_text(json.dumps(sorted(sent), indent=2) + "\n")
        card, expires, days = keys[0].split(":")  # the closest threshold crossed
        note = run_miles.card_note(card, balances.get(card, 0), expires[:7],
                                   f"expiry reminder sent ({days}-day threshold)")
        vault.log("⏰", "Expiry reminder sent", f"{card} expires end {expires[:7]}, {days}-day reminder", note)
    return len(reminders)
