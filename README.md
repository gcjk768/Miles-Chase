# Miles Chase

Posts two KrisFlyer reports to a Telegram channel, written by Claude:

- **Daily** (07:53 SGT): SIA cash fares for your watchlist vs Saver awards, with a verdict per route.
- **Monthly** (1st of the month, 08:07 SGT): balances, expiry watch, what you can book, deals and tips.

A GitHub Actions workflow runs `run_miles.py` on schedule. The script fills today's date, your balances,
last month's history and yesterday's prices into `miles_monthly.txt` / `miles_daily.txt`, runs
`claude -p` with web search, posts the result to Telegram, and commits the history to `state/`.

## Setup

1. **Make this repository private.** The reports and `state/` contain your balances, so the workflow
   refuses to run in a public repo. Settings > General > Danger Zone > Change visibility.
2. **Create a Telegram bot.** Message @BotFather, send `/newbot` and copy the token. Add the bot to your
   private channel as an admin. To get the channel's chat ID, post something in the channel, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` (it starts with `-100`).
3. **Add secrets** under Settings > Secrets and variables > Actions:
   - `ANTHROPIC_API_KEY` from console.anthropic.com
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - Optional: a variable (not secret) `CLAUDE_MODEL` to pick a model.
4. **Fill in your data** and replace every `[placeholder]`:
   - `data/my_points.txt`: your balances. Update it each month before the 1st.
   - `data/watchlist.txt`: routes to track and your total `MY MILES`.
5. **Test it**: Actions > Miles reports > Run workflow, pick `daily` or `monthly`.

## Files

| File | What it is | Who edits it |
| --- | --- | --- |
| `miles_monthly.txt`, `miles_daily.txt` | The prompts. Update the baseline facts when a report lists "Baseline changes". | You |
| `data/my_points.txt`, `data/watchlist.txt` | Your inputs. Lines starting with `#` are ignored. | You |
| `data/fare_data.txt` | Optional fares from a flight price API. If present, Claude uses it instead of searching. | You or a future script |
| `state/` | Monthly balance history and daily PRICES lines. | The workflow |

## Running locally

Needs Python 3.9+ and Claude Code (`npm install -g @anthropic-ai/claude-code`), signed in or with
`ANTHROPIC_API_KEY` set.

```sh
python run_miles.py daily --dry-run   # show the assembled prompt, no Claude call
python run_miles.py daily --no-send   # run Claude and print instead of posting
python run_miles.py monthly           # posts to Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
```

A local run also writes to `state/`, so commit it or discard it to keep the workflow's history clean.
