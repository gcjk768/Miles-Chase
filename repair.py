"""Self-repair: when a job fails, ask `claude -p` to find the cause and fix what it safely can.

Claude may read the whole project but can only edit files in data/ and state/, such as a
malformed balance line or a corrupted history file. It can't change code, prompts or .env, and
it must never invent balances. Anything else it explains, so you know what to do.
"""

import os
import subprocess

import run_miles

TOOLS = ["Read", "Glob", "Grep", "Edit(data/**)", "Edit(state/**)"]
TIMEOUT_SECONDS = 10 * 60
PROMPT = """You are fixing a failed job in the Miles Chase app, running on the user's NAS.
The project is the current folder: run_miles.py (report runner), scheduler.py, telegram_bot.py,
reminders.py, news_watch.py, the prompts miles_daily.txt and miles_monthly.txt, data/ (the user's
balances and watchlist) and state/ (history written by the app).

The command `python3 run_miles.py {mode}` failed with this output:
<output>
{output}
</output>

Find the cause. You may only edit files in data/ and state/: fix a malformed line, a leftover
[placeholder] you can resolve without guessing, or a corrupted state file (resetting it is fine).
Keep the user's real numbers and never invent a balance, expiry or route. Never edit code, prompts
or .env. If the cause is in code, the network, Claude sign-in or anything that needs the user,
change nothing and say what the user should do.

Reply in at most 6 short lines of plain text for Telegram, no markdown, exactly this shape:
Cause: <one line>
Fixed: <what you changed, or "nothing">
You need to: <what the user should do, or "nothing">
"""


def repair(mode, output):
    """Run the repair and return Claude's short summary."""
    cmd = [os.environ.get("CLAUDE_BIN") or "claude", "-p", "--allowedTools", *TOOLS]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
    prompt = PROMPT.format(mode=mode, output="\n".join(output.splitlines()[-40:]))
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS, cwd=run_miles.ROOT)
    summary = result.stdout.strip()
    if result.returncode != 0 or not summary:
        raise RuntimeError(f"claude exited with code {result.returncode}: {result.stderr.strip()[-300:]}")
    return summary
