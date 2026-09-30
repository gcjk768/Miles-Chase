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
CLAUDE_TIMEOUT_SECONDS = 20 * 60

SGT = timezone(timedelta(hours=8))
PRICES_LINE = re.compile(r"^PRICES (\d{4}-\d{2}-\d{2})\b.*$")


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
    miles = [l for l in lines if l.upper().startswith("MY MILES:")]
    routes = [l for l in lines if not l.upper().startswith("MY MILES:")]
    if not routes:
        fail("data/watchlist.txt has no routes")
    if len(miles) != 1:
        fail("data/watchlist.txt needs exactly one 'MY MILES: <number>' line")

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
        miles[0],
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


def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for piece in chunks(text):
        body = json.dumps({
            "chat_id": chat_id,
            "text": piece,
            "disable_web_page_preview": True,
        }).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.load(response)
        if not reply.get("ok"):
            fail(f"Telegram rejected the message: {reply}")


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

    report = run_claude(prompt)

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
    send_telegram(report, token, chat_id)
    print(f"Posted the {args.mode} report to Telegram ({len(report)} characters).")


if __name__ == "__main__":
    main()
