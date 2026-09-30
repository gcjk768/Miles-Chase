# Miles Chase

Posts two KrisFlyer reports to a Telegram channel, written by Claude:

- **Daily** (07:53 SGT): SIA cash fares for your watchlist vs Saver awards, with a verdict per route.
  On quiet days (no fare changes or deals) it posts one short line instead; see `DAILY_QUIET`.
- **Monthly** (1st of the month, 08:07 SGT): balances, expiry watch, what you can book, deals and tips.
- **Expiry reminders** (09:00 SGT, NAS): a message 60, 30 and 7 days before any points or miles expire.
  No Claude needed.

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
   - `data/watchlist.txt`: routes to track. `MY MILES` is worked out from your card points before
     transfer: Citi Rewards and SC points at 25,000 = 10,000 miles (so 52,000 points = 20,800 miles),
     PremierMiles and KrisFlyer 1:1. Both reports use the same total. Transfer fees are one per card
     (S$27.25 Citi Rewards, S$27.25 PremierMiles, about S$27 SC; set in `TRANSFER_FEES` in `run_miles.py`). Add a `MY MILES: <number>` line
     to the watchlist to override it.
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
0 9 * * *  cd /path/to/Miles-Chase && /usr/bin/python3 run_miles.py reminders >> miles.log 2>&1
```

Those times assume the machine's clock is on Singapore time. Moving from computer to NAS: copy the
folder including `.env`, `data/` and `state/` so the history comes along. On a headless NAS, sign in
with `claude setup-token` on your computer and put `CLAUDE_CODE_OAUTH_TOKEN=...` in the NAS's `.env`.

If you run it this way, disable the GitHub workflow (Actions > Miles reports > ... > Disable workflow)
so it doesn't also run and fail every morning.

## UGREEN NAS (DXP4800 Pro, UGOS Pro) with Docker

The container runs `scheduler.py`, which posts the daily report at 07:53 and the monthly report at
08:07 on the 1st, Singapore time. If the NAS was off at that time, it catches up the same day.
Failures are posted to Telegram too.

1. Test on your computer first (see above), so `.env`, `data/` and `state/` are filled in and working.
   On the NAS, use a subscription token: run `claude setup-token` on your computer and add
   `CLAUDE_CODE_OAUTH_TOKEN=...` to `.env`. Don't set `CLAUDE_BIN` for Docker.
2. Install the **Docker** app from the UGOS App Center.
3. Copy the whole `Miles-Chase` folder, including `.env`, to a shared folder on the NAS,
   e.g. `docker/Miles-Chase`, using the UGOS Files app or SMB from your computer.
4. Start it, either:
   - In the Docker app: Project > Create, choose the `Miles-Chase` folder as the path so it picks up
     `docker-compose.yml`, then deploy. Or:
   - Over SSH (Control Panel > Terminal > enable SSH): `cd` to the folder and run
     `sudo docker compose up -d --build`.
5. Check the container log shows `scheduler started`. To send a report right now as a test:
   `sudo docker exec miles-chase python3 run_miles.py daily`

Update your balances by editing `data/my_points.txt` on the NAS share; no restart needed. After changing
code with `git pull`, restart the container. To update Claude Code, rebuild the image
(`sudo docker compose build --no-cache && sudo docker compose up -d`).

## Telegram commands (NAS only)

While the container runs, the bot answers commands posted in your channel (`TELEGRAM_CHAT_ID`).
Messages from any other chat are ignored.

| Command | What it does |
| --- | --- |
| `/points` | Show your balances, the miles they're worth before transfer and the fees to transfer them |
| `/points CR 52000` | Set a balance (`CR`, `CPM`, `SCR` or `KF`); the expiry is kept |
| `/points SCR 31000 exp 2027-06` | Set a balance and its expiry |
| `/watch` | List the watchlist routes, numbered |
| `/watch add SIN Bali, Jun 2027` | Add a route |
| `/watch remove 2` | Remove route number 2 |
| `/goal` | Show your goal |
| `/goal Tokyo business, 2 pax, Mar 2027` | Set your goal (used by the monthly report) |
| `/goal clear` | Remove your goal |
| `/run daily`, `/run monthly` | Run a report now (the daily one always posts in full) |
| `/help` | List the commands |

## Safety checks before posting

Before anything is posted, the runner hides card-like and long account-like numbers (10 or more digits)
outside links and removes markdown the prompt forbids. If a report is over the prompt's limit
(2,000 characters daily, 3,500 monthly), Claude is asked once more, without web search, to shorten it
while keeping every number. If it's still too long for one Telegram message, it's posted in parts.

## Quiet days and expiry reminders

The daily report starts with a `STATUS: NEWS` or `STATUS: QUIET` line that the runner removes. On a
quiet day (no alert, no fare change) it follows `DAILY_QUIET` in `.env`: `line` (default) posts one
short line, `silent` posts nothing, `off` always posts the full report. `/run daily` always posts in full.

Expiry reminders read the `exp YYYY-MM` dates in `data/my_points.txt`, count each as the end of that
month, and send one message at 60, 30 and 7 days before. Each goes out once
(`state/reminders_sent.json`). Preview with `python3 run_miles.py reminders --dry-run`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They need no network, Claude or Telegram, and run on GitHub on every push.
