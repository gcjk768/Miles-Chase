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
   - `CLAUDE_CODE_OAUTH_TOKEN`: runs `claude -p` on your Claude Pro or Max subscription. On your own computer,
     install Claude Code (`npm install -g @anthropic-ai/claude-code`), run `claude setup-token` and paste the
     token it prints. Or use `ANTHROPIC_API_KEY` from console.anthropic.com to pay per use instead.
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

## Run on your computer or a NAS (no GitHub Actions)

Needs Python 3.9+, Node.js 18+ and Claude Code. Nothing leaves your machine except the Claude call
and the Telegram post, so the repo can stay public as long as you don't push `data/` or `state/` changes.

```sh
git clone https://github.com/gcjk768/Miles-Chase.git && cd Miles-Chase
npm install -g @anthropic-ai/claude-code
claude                      # sign in once with your Claude subscription, then /exit
cp .env.example .env        # add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
# edit data/my_points.txt and data/watchlist.txt

python3 run_miles.py daily --dry-run   # check the assembled prompt, no Claude call
python3 run_miles.py daily --no-send   # run Claude, print instead of posting
python3 run_miles.py daily             # run Claude and post to Telegram
```

To schedule it, add these lines with `crontab -e` (or the NAS's task scheduler). Cron has a minimal
PATH, so set `CLAUDE_BIN` in `.env` to the output of `which claude`, and use full paths below:

```cron
53 7 * * * cd /path/to/Miles-Chase && /usr/bin/python3 run_miles.py daily >> miles.log 2>&1
7 8 1 * *  cd /path/to/Miles-Chase && /usr/bin/python3 run_miles.py monthly >> miles.log 2>&1
```

Those times assume the machine's clock is on Singapore time. Moving from computer to NAS: copy the
folder including `.env`, `data/` and `state/` so the history comes along. On a headless NAS, sign in
with `claude setup-token` on your computer and put `CLAUDE_CODE_OAUTH_TOKEN=...` in the NAS's `.env`.

If you run it this way, disable the GitHub workflow (Actions > Miles reports > ... > Disable workflow)
so it doesn't also run and fail every morning.
