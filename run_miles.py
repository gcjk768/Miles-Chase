#!/usr/bin/env python3
"""Run the KrisFlyer miles prompts through Claude Code and post the result to Telegram.

Usage:
    python run_miles.py daily      # daily fare tracker
    python run_miles.py monthly    # monthly miles coach report

Options:
    --dry-run   print the assembled prompt and stop (no Claude call, nothing saved)
    --no-send   run Claude and save state, but print instead of posting to Telegram

Environment (or a .env file next to this script, see .env.example):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   where to post (if unset, the report is printed)
    CLAUDE_MODEL                           optional model override for `claude -p`
    CLAUDE_BIN                             path to `claude` if it isn't on PATH (e.g. under cron)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = ROOT / "state"

PROMPTS = {"daily": ROOT / "miles_daily.txt", "monthly": ROOT / "miles_monthly.txt"}
MY_POINTS = DATA / "my_points.txt"
WATCHLIST = DATA / "watchlist.txt"
FARE_DATA = DATA / "fare_data.txt"
MONTHLY_HISTORY = STATE / "monthly_history.txt"
PRICES_HISTORY = STATE / "prices.txt"

SEPARATOR = "=============================="
HISTORY_MONTHS_KEPT = 24
PRICES_DAYS_KEPT = 400
TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message
LENGTH_TARGETS = {"daily": 2000, "monthly": 4000}  # the limits the prompts ask for
CLAUDE_TIMEOUT_SECONDS = 20 * 60

SGT = timezone(timedelta(hours=8))
# Claude sometimes copies the brackets from the prompt's format example: PRICES [2026-09-30] | ...
PRICES_LINE = re.compile(r"^PRICES \[?(\d{4}-\d{2}-\d{2})\]?(?=\s|$).*$")

# Bank points to KrisFlyer miles: (points per transfer block, miles per block, minimum points).
# Only whole blocks count towards MY MILES, since that's what can actually be transferred.
CONVERSIONS = {
    "CR": (25000, 10000, 25000),   # Citi Rewards
    "SCR": (25000, 10000, 25000),  # Standard Chartered Rewards
    "CPM": (1, 1, 10000),          # Citi PremierMiles, 1:1 with a 10,000 minimum
    "KF": (1, 1, 0),               # already in KrisFlyer
}
BALANCE_LINE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*([\d,]+)")

# Card numbers (groups of 4) and long digit runs such as KrisFlyer or account numbers.
ACCOUNT_NUMBER = re.compile(r"\b(?:\d{4}[ -]){3}\d{1,7}\b|\b\d{10,19}\b")
URL = re.compile(r"https?://\S+")


def load_env_file(path=ROOT / ".env"):
    """Set KEY=value lines from .env, without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def fail(message):
    sys.exit(f"error: {message}")


def read_lines(path):
    """Non-empty, non-comment lines of a file, or [] if it doesn't exist."""
    if not path.exists():
        return []
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def check_filled_in(path, lines):
    """Stop if a data file still has template placeholders like [points]."""
    if not lines:
        fail(f"{path.relative_to(ROOT)} is empty. Fill it in first (see README.md).")
    for line in lines:
        if re.search(r"\[[^\]]*\]", line):
            fail(f"{path.relative_to(ROOT)} still has a placeholder: {line!r}")


def parse_balances(lines):
    """{card: points} from lines like 'CR: 50,000 exp 2027-01'."""
    balances = {}
    for line in lines:
        match = BALANCE_LINE.match(line)
        if match and match.group(1).upper() in CONVERSIONS:
            balances[match.group(1).upper()] = int(match.group(2).replace(",", ""))
    return balances


def miles_available(balances):
    """KrisFlyer miles you could have now: KF miles plus whole transfer blocks of bank points."""
    total = 0
    for card, points in balances.items():
        per_block, miles_per_block, minimum = CONVERSIONS[card]
        if points >= minimum:
            total += points // per_block * miles_per_block
    return total


def set_balance(card, points, expiry=None):
    """Update one card's line in data/my_points.txt, keeping its expiry unless a new one is given."""
    card = card.upper()
    if card not in CONVERSIONS:
        raise ValueError(f"unknown card {card}, use one of {', '.join(CONVERSIONS)}")
    lines = MY_POINTS.read_text(encoding="utf-8").splitlines() if MY_POINTS.exists() else []
    for i, line in enumerate(lines):
        match = re.match(rf"^\s*{card}\s*:(.*)$", line, re.IGNORECASE)
        if match:
            old = re.search(r"\bexp\s+(\S+)", match.group(1))
            if not expiry and old and "[" not in old.group(1):
                expiry = old.group(1)
            lines[i] = f"{card}: {points}" + (f" exp {expiry}" if expiry else "")
            break
    else:
        lines.append(f"{card}: {points}" + (f" exp {expiry}" if expiry else ""))
    write_lines(MY_POINTS, lines)


def clean_report(report, mode):
    """Last safety net before posting: hide account-like numbers, strip markdown, check length."""
    parts = []
    last = 0
    for url in URL.finditer(report):  # leave links intact
        parts.append(ACCOUNT_NUMBER.sub("[number hidden]", report[last:url.start()]))
        parts.append(url.group())
        last = url.end()
    parts.append(ACCOUNT_NUMBER.sub("[number hidden]", report[last:]))
    report = "".join(parts)
    report = report.replace("**", "")
    report = re.sub(r"^#+\s*", "", report, flags=re.MULTILINE)
    target = LENGTH_TARGETS[mode]
    if len(report) > target:
        print(f"warning: the {mode} report is {len(report)} characters, over the {target} target; "
              "posting it in parts", file=sys.stderr)
    return report


def instructions(mode):
    """The instruction part of a prompt file, i.e. everything above the separator."""
    text = PROMPTS[mode].read_text(encoding="utf-8")
    if SEPARATOR not in text:
        fail(f"{PROMPTS[mode].name} has no '{SEPARATOR}' separator line")
    return text.split(SEPARATOR, 1)[0].rstrip()


def build_monthly(today):
    points = read_lines(MY_POINTS)
    check_filled_in(MY_POINTS, points)
    history = read_lines(MONTHLY_HISTORY)
    this_month = today.strftime("%Y-%m")
    # On a re-run within the same month, don't compare the month with itself.
    history = [h for h in history if not h.startswith(this_month)]

    prompt = "\n".join([
        instructions("monthly"),
        "",
        SEPARATOR,
        f"TODAY: {today.isoformat()}",
        "",
        "MY POINTS THIS MONTH",
        *points,
        "",
        "HISTORY (previous months, oldest first, filled by the script)",
        *history,
        "",
    ])
    return prompt, points


def build_daily(today):
    lines = read_lines(WATCHLIST)
    check_filled_in(WATCHLIST, lines)
    override = [l for l in lines if l.upper().startswith("MY MILES:")]
    routes = [l for l in lines if not l.upper().startswith("MY MILES:")]
    if not routes:
        fail("data/watchlist.txt has no routes")
    if override:
        miles = override[-1]
    else:
        points = read_lines(MY_POINTS)
        check_filled_in(MY_POINTS, points)
        miles = f"MY MILES: {miles_available(parse_balances(points))}"

    fare_data = read_lines(FARE_DATA)
    # YESTERDAY is the latest saved PRICES line from before today, so a re-run
    # later the same day still compares against yesterday.
    earlier = [
        l for l in read_lines(PRICES_HISTORY)
        if (m := PRICES_LINE.match(l)) and m.group(1) < today.isoformat()
    ]
    yesterday = earlier[-1:] if earlier else []

    return "\n".join([
        instructions("daily"),
        "",
        SEPARATOR,
        f"TODAY: {today.isoformat()}",
        "",
        "WATCHLIST",
        *routes,
        "",
        miles,
        "",
        "FARE_DATA (optional, filled by the script from a flight price API)",
        *fare_data,
        "",
        "YESTERDAY (filled by the script)",
        *yesterday,
        "",
    ])


def run_claude(prompt):
    cmd = [os.environ.get("CLAUDE_BIN") or "claude", "-p", "--allowedTools", "WebSearch", "WebFetch"]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        fail("the `claude` command isn't installed (npm install -g @anthropic-ai/claude-code)")
    except subprocess.TimeoutExpired:
        fail(f"Claude didn't finish within {CLAUDE_TIMEOUT_SECONDS // 60} minutes")
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        sys.stderr.write(result.stderr)
        fail(f"Claude exited with code {result.returncode} and output {output[:500]!r}")
    return output


def split_prices_line(report):
    """Return (report without its PRICES line, the PRICES line or None)."""
    lines = report.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if PRICES_LINE.match(lines[i].strip()):
            prices = lines[i].strip()
            return "\n".join(lines[:i] + lines[i + 1:]).rstrip(), prices
    return report, None


def save_prices(prices, today):
    if not prices:
        print("warning: no PRICES line in the output, so tomorrow has nothing to compare to",
              file=sys.stderr)
        return
    prices = re.sub(r"^PRICES \[(\d{4}-\d{2}-\d{2})\]", r"PRICES \1", prices)
    date = PRICES_LINE.match(prices).group(1)
    if date != today.isoformat():
        print(f"warning: PRICES line is dated {date}, expected {today}; saving it under today",
              file=sys.stderr)
        prices = prices.replace(date, today.isoformat(), 1)
    kept = [l for l in read_lines(PRICES_HISTORY) if not l.startswith(f"PRICES {today}")]
    write_lines(PRICES_HISTORY, (kept + [prices])[-PRICES_DAYS_KEPT:])


def save_monthly_history(points, today):
    month = today.strftime("%Y-%m")
    entry = f"{month}: " + "; ".join(points)
    kept = [l for l in read_lines(MONTHLY_HISTORY) if not l.startswith(month)]
    write_lines(MONTHLY_HISTORY, (kept + [entry])[-HISTORY_MONTHS_KEPT:])


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def chunks(text, limit=TELEGRAM_LIMIT):
    """Split text on line breaks into pieces Telegram will accept."""
    pieces, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:  # a single overlong line
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def telegram_api(token, method, payload, timeout=30):
    """Call a Telegram Bot API method and return its result, or raise RuntimeError."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram {method} failed: {error.read().decode(errors='replace')}")
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {reply}")
    return reply["result"]


def send_telegram(text, token, chat_id):
    for piece in chunks(text):
        telegram_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": piece,
            "disable_web_page_preview": True,
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=sorted(PROMPTS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args()
    load_env_file()

    today = datetime.now(SGT).date()
    if args.mode == "monthly":
        prompt, points = build_monthly(today)
    else:
        prompt = build_daily(today)

    if args.dry_run:
        print(prompt)
        return

    report = clean_report(run_claude(prompt), args.mode)

    if args.mode == "daily":
        report, prices = split_prices_line(report)
        save_prices(prices, today)
    else:
        save_monthly_history(points, today)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if args.no_send or not (token and chat_id):
        print(report)
        return
    try:
        send_telegram(report, token, chat_id)
    except (RuntimeError, OSError) as error:
        fail(str(error))
    print(f"Posted the {args.mode} report to Telegram ({len(report)} characters).")


if __name__ == "__main__":
    main()
