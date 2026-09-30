#!/usr/bin/env python3
"""Run the miles reports on schedule, for a NAS or any always-on machine.

Runs `run_miles.py daily` at 07:53 and `run_miles.py monthly` at 08:07 on the 1st,
Singapore time. If the machine was off at that time, the report runs as soon as it's
back, as long as it's still the same day. Failures are posted to Telegram.
Between reports it answers Telegram commands such as /points (see telegram_bot.py).
"""

import json
import subprocess
import sys
import time
from datetime import datetime, time as clock

import run_miles
import telegram_bot

DAILY_AT = clock(7, 53)
MONTHLY_AT = clock(8, 7)
LAST_RUN = run_miles.STATE / "scheduler.json"
CHECK_EVERY_SECONDS = 30
TELEGRAM_WAIT_SECONDS = 20


def due_jobs(now, last_run):
    """Jobs that should run now, as (mode, key) pairs; key marks the run as done."""
    jobs = []
    today = now.date().isoformat()
    if now.time() >= DAILY_AT and last_run.get("daily") != today:
        jobs.append(("daily", today))
    month = now.strftime("%Y-%m")
    if now.day == 1 and now.time() >= MONTHLY_AT and last_run.get("monthly") != month:
        jobs.append(("monthly", month))
    return jobs


def load_last_run(now):
    if LAST_RUN.exists():
        return json.loads(LAST_RUN.read_text())
    # First start: don't fire a report straight away just because today's time has passed.
    last_run = {mode: key for mode, key in due_jobs(now, {})}
    save_last_run(last_run)
    return last_run


def save_last_run(last_run):
    LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps(last_run, indent=2) + "\n")


def log(message):
    print(f"{datetime.now(run_miles.SGT):%Y-%m-%d %H:%M:%S} {message}", flush=True)


def run(mode):
    log(f"starting {mode} report")
    result = subprocess.run(
        [sys.executable, str(run_miles.ROOT / "run_miles.py"), mode],
        capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        log(f"{mode} report done: {output.splitlines()[-1] if output else ''}")
        return
    log(f"{mode} report failed (exit {result.returncode}):\n{output}")
    notify_failure(mode, output)


def notify_failure(mode, output):
    token = run_miles.os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = run_miles.os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    tail = "\n".join(output.splitlines()[-10:])
    try:
        run_miles.send_telegram(f"⚠️ Miles {mode} report failed.\n{tail}", token, chat_id)
    except (RuntimeError, OSError) as error:
        log(f"couldn't post the failure to Telegram: {error}")


def main():
    run_miles.load_env_file()
    last_run = load_last_run(datetime.now(run_miles.SGT))
    log(f"scheduler started: daily at {DAILY_AT:%H:%M}, monthly on the 1st at {MONTHLY_AT:%H:%M} (SGT)")
    token = run_miles.os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = run_miles.os.environ.get("TELEGRAM_CHAT_ID")
    while True:
        if token and chat_id:
            try:
                # Doubles as the wait between checks: returns early when a message arrives.
                telegram_bot.poll(token, chat_id, run, timeout=TELEGRAM_WAIT_SECONDS)
            except (RuntimeError, OSError, ValueError) as error:
                log(f"Telegram commands unavailable: {error}")
                time.sleep(CHECK_EVERY_SECONDS)
        else:
            time.sleep(CHECK_EVERY_SECONDS)
        now = datetime.now(run_miles.SGT)
        for mode, key in due_jobs(now, last_run):
            # Mark first, so a failing report isn't retried every 30 seconds.
            last_run[mode] = key
            save_last_run(last_run)
            run(mode)


if __name__ == "__main__":
    main()
