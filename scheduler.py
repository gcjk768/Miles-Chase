#!/usr/bin/env python3
"""Run the miles jobs on schedule, for a NAS or any always-on machine.

Singapore time:
    18:00 daily       `run_miles.py daily`, fare tracker + region deals (Claude); silent if nothing new
    08:07 on the 1st  `run_miles.py monthly`, the coach report (Claude)
    09:00 daily       `run_miles.py reminders`, expiry reminders (no Claude)
    every hour, 24/7  `run_miles.py news` + `run_miles.py promos`: new blog posts / videos and new
                      KrisFlyer / KrisShop promos; Claude (opus, then sonnet) judges what matters

If the machine was off at a job's time, the job runs as soon as it's back, as long as it's
still the same day. Between jobs it answers Telegram commands such as /points.

Keeping itself healthy:
- Temporary errors (network, timeouts, Claude busy) are retried up to 3 times, 15 minutes apart.
- Other failures get one self-repair attempt a day with `claude -p` (see repair.py), then the job
  is re-run once. What happened is posted to Telegram.
- An error in the scheduler itself is logged and reported, and the loop carries on.
- A heartbeat file is written while it's alive; the Docker healthcheck restarts the container
  if it goes quiet (see healthcheck.py).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, time as clock

import run_miles
import telegram_bot
import vault

DAILY_AT = clock(18, 0)
MONTHLY_AT = clock(8, 7)
REMINDERS_AT = clock(9, 0)
LAST_RUN = run_miles.STATE / "scheduler.json"
HEARTBEAT = run_miles.STATE / "heartbeat"
CHECK_EVERY_SECONDS = 30
TELEGRAM_WAIT_SECONDS = 20
JOB_TIMEOUT_SECONDS = 45 * 60
RETRY_AFTER_SECONDS = 15 * 60
MAX_RETRIES = 3
CLAUDE_MODES = ("daily", "monthly", "ask", "tickets")
ERROR_REPORT_EVERY_SECONDS = 60 * 60  # at most one message an hour about the same problem

TRANSIENT = re.compile(
    r"timed? ?out|timeout|temporar|connection|network|unreachable|name resolution|urlopen error"
    r"|\b(429|500|502|503|504|529)\b|overloaded|rate.?limit|too many requests|try again",
    re.IGNORECASE)
SIGN_IN = re.compile(
    r"\b401\b|unauthori[sz]ed|invalid (api )?key|authentication|oauth|not logged in|/login"
    r"|credit balance|expired token", re.IGNORECASE)

retries = {}        # mode -> (next try as a timestamp, tries so far, options)
repaired_today = {}  # mode -> date of the last self-repair
last_reported = {}  # problem -> when it was last posted


def env(name, default):
    return os.environ.get(name, default).strip().lower()


def news_every_minutes():
    try:
        return max(5, int(env("NEWS_EVERY_MINUTES", "60")))
    except ValueError:
        return 60


def due_jobs(now, last_run):
    """Jobs that should run now, as (mode, key) pairs; key marks the run as done."""
    jobs = []
    today = now.date().isoformat()
    if now.time() >= DAILY_AT and last_run.get("daily") != today:
        jobs.append(("daily", today))
    month = now.strftime("%Y-%m")
    if now.day == 1 and now.time() >= MONTHLY_AT and last_run.get("monthly") != month:
        jobs.append(("monthly", month))
    if now.time() >= REMINDERS_AT and last_run.get("reminders") != today:
        jobs.append(("reminders", today))
    if env("NEWS_ALERTS", "on") != "off":
        slot = f"{today} {(now.hour * 60 + now.minute) // news_every_minutes()}"
        for mode in ("news", "promos"):
            if last_run.get(mode) != slot:
                jobs.append((mode, slot))
    return jobs


def load_last_run(now):
    try:
        if LAST_RUN.exists():
            last_run = json.loads(LAST_RUN.read_text())
            if isinstance(last_run, dict):
                return last_run
    except (ValueError, OSError) as error:
        log(f"scheduler.json unreadable ({error}); starting fresh")
    # First start: don't fire a report straight away just because today's time has passed.
    last_run = {mode: key for mode, key in due_jobs(now, {}) if mode not in ("news", "promos")}
    save_last_run(last_run)
    return last_run


def save_last_run(last_run):
    LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps(last_run, indent=2) + "\n")


def beat():
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(f"{time.time():.0f}\n")
    except OSError:
        pass


def log(message):
    print(f"{datetime.now(run_miles.SGT):%Y-%m-%d %H:%M:%S} {message}", flush=True)


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        run_miles.send_telegram(text, token, chat_id)
    except (RuntimeError, OSError) as error:
        log(f"couldn't post to Telegram: {error}")


def failure(mode, headline, *blocks, log_text=""):
    """A failure alert as an HTML card; the job's last log lines go in the expandable quote."""
    return run_miles.card("error", mode, f"❌ {run_miles.esc(headline)}", *blocks,
                          run_miles.background("Log", log_text) if log_text else "")


def report_once_an_hour(problem, text):
    if time.time() - last_reported.get(problem, 0) >= ERROR_REPORT_EVERY_SECONDS:
        last_reported[problem] = time.time()
        telegram(text)


def show_typing():
    """Telegram's "typing…" lasts about 5 seconds, so this is repeated while Claude works."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            run_miles.send_typing(token, chat_id)
        except (RuntimeError, OSError):
            pass  # cosmetic; never let it break the job


def execute(args, timeout=JOB_TIMEOUT_SECONDS, typing=False):
    """Run a command, keeping the heartbeat going. Returns (exit code, output); -1 on timeout."""
    with tempfile.TemporaryFile("w+") as out:
        process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, text=True,
                                   cwd=run_miles.ROOT)
        started = time.time()
        while process.poll() is None:
            beat()
            if typing:
                show_typing()
            if time.time() - started > timeout:
                process.kill()
                process.wait()
                out.seek(0)
                return -1, out.read() + f"\njob timed out after {timeout // 60} minutes"
            time.sleep(5)
        out.seek(0)
        return process.returncode, out.read().strip()


def run_job(mode, *options):
    # Claude jobs take a minute or more, so show "typing…" in the chat meanwhile.
    return execute([sys.executable, str(run_miles.ROOT / "run_miles.py"), mode, *options],
                   typing=mode in CLAUDE_MODES)


def run(mode, *options, attempt=1):
    """Run a job and deal with failure: retry, self-repair or report."""
    log(f"starting {mode}" + (f" (try {attempt})" if attempt > 1 else ""))
    code, output = run_job(mode, *options)
    if code == 0:
        retries.pop(mode, None)
        log(f"{mode} done: {output.splitlines()[-1] if output else ''}")
        return
    log(f"{mode} failed (exit {code}):\n{output}")
    vault.log("⚠️", f"{mode} failed", f"exit {code}, try {attempt}")
    tail = "\n".join(output.splitlines()[-6:])

    if SIGN_IN.search(output) and not TRANSIENT.search(output):
        report_once_an_hour(f"sign-in {mode}", failure(
            mode, "Claude sign-in problem.",
            "🔑 Run <code>claude setup-token</code> on your computer, put the new token in "
            "<code>.env</code> on the NAS as <code>CLAUDE_CODE_OAUTH_TOKEN</code>, "
            "then restart the container.", log_text=tail))
        return

    if mode in ("ask", "tickets"):  # on demand: no retries or self-repair, just say it failed
        telegram(failure(mode, f"/{mode} failed.", "<i>Try again in a few minutes.</i>", log_text=tail))
        return

    if TRANSIENT.search(output) or code == -1:
        if attempt <= MAX_RETRIES:
            retries[mode] = (time.time() + RETRY_AFTER_SECONDS, attempt + 1, options)
            log(f"{mode}: temporary problem, retrying in {RETRY_AFTER_SECONDS // 60} minutes")
            return
        retries.pop(mode, None)
        telegram(failure(mode, f"Failed {attempt} times with a temporary problem.",
                         "<i>It will run again at its next scheduled time.</i>", log_text=tail))
        return

    retries.pop(mode, None)
    today = datetime.now(run_miles.SGT).date()
    if env("SELF_REPAIR", "on") == "off" or repaired_today.get(mode) == today:
        report_once_an_hour(f"failed {mode}", failure(mode, "Failed.", log_text=tail))
        return

    repaired_today[mode] = today
    log(f"{mode}: asking Claude to repair")
    import repair
    try:
        summary = repair.repair(mode, output)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        telegram(failure(mode, "Failed.", f"🔧 Self-repair couldn't run: {run_miles.esc(error)}",
                         log_text=tail))
        return
    code, retry_output = run_job(mode, *options)
    outcome = "✅ Re-ran it and it worked." if code == 0 else "❌ Re-ran it and it still fails."
    if code != 0:
        tail += "\n\nAfter repair:\n" + "\n".join(retry_output.splitlines()[-4:])
    log(f"{mode} after repair: exit {code}")
    vault.log("🔧", "Self-repair", f"{mode}: {summary} → {'fixed' if code == 0 else 'still failing'}")
    telegram(failure(mode, "Failed.", f"🔧 <b>Self-repair by Claude</b>\n{run_miles.esc(summary)}",
                     outcome, log_text=tail))


def due_retries():
    now = time.time()
    return [(mode, attempt, options) for mode, (at, attempt, options) in list(retries.items())
            if at <= now]


def tick(last_run, token, chat_id):
    if token and chat_id:
        try:
            # Doubles as the wait between checks: returns early when a message arrives.
            telegram_bot.poll(token, chat_id, run, timeout=TELEGRAM_WAIT_SECONDS)
        except (RuntimeError, OSError, ValueError) as error:
            log(f"Telegram commands unavailable: {error}")
            time.sleep(CHECK_EVERY_SECONDS)
    else:
        time.sleep(CHECK_EVERY_SECONDS)
    beat()
    now = datetime.now(run_miles.SGT)
    for mode, key in due_jobs(now, last_run):
        # Mark first, so a failing job isn't started again every 30 seconds.
        last_run[mode] = key
        save_last_run(last_run)
        run(mode)
    for mode, attempt, options in due_retries():
        retries.pop(mode, None)
        run(mode, *options, attempt=attempt)


def main():
    run_miles.load_env_file()
    beat()
    moved = vault.migrate()
    if moved:
        log(f"vault: moved {moved} Activity note(s) into Activity/YYYY/MM/")
    last_run = load_last_run(datetime.now(run_miles.SGT))
    log(f"scheduler started: daily {DAILY_AT:%H:%M}, monthly on the 1st {MONTHLY_AT:%H:%M}, "
        f"reminders {REMINDERS_AT:%H:%M}, news + promos every {news_every_minutes()} min (SGT)")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    while True:
        try:
            tick(last_run, token, chat_id)
        except Exception as error:  # keep going whatever happens; report it
            log("scheduler error:\n" + traceback.format_exc())
            report_once_an_hour(f"scheduler {type(error).__name__}",
                                failure("scheduler", "Hit an error and carried on.",
                                        log_text=str(error)))
            time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
