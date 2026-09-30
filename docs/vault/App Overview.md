---
tags: [active]
updated: 2026-09-30
---
# App Overview

One container (`miles-chase`), one loop: [scheduler.py](../../scheduler.py) runs jobs on time (SGT) and long-polls Telegram between them.

| File | Role |
|---|---|
| [run_miles.py](../../run_miles.py) | Job runner: `daily`, `monthly` (Claude + web search), `ask` (Claude), `reminders`, `news` (no Claude). Telegram send helpers, `split_chat_id` for `chat/topic` ids. |
| [scheduler.py](../../scheduler.py) | Schedule, retries (transient errors ×3), one self-repair a day per job, heartbeat. `/ask` failures skip retry/repair. |
| [telegram_bot.py](../../telegram_bot.py) | Commands `/points /watch /goal /run /ask /help`; only reads its own chat (and topic, if set). |
| [repair.py](../../repair.py) | `claude -p` self-repair, may only edit `data/` and `state/`. |
| [healthcheck.py](../../healthcheck.py) | Docker healthcheck; kills a stuck scheduler so Docker restarts it. Unhealthy/restarting also trips NAS Doctor. |
| [news_watch.py](../../news_watch.py), [reminders.py](../../reminders.py) | Blog deal alerts, expiry reminders. |
| [miles_daily.txt](../../miles_daily.txt), [miles_monthly.txt](../../miles_monthly.txt) | Prompts. |
| [data/](../../data/) | User balances + watchlist (edited via Telegram). Never overwritten by [deploy.sh](../../deploy.sh) after first deploy. |
| [state/](../../state/) | History, offsets, heartbeat. |

**Claude sign-in:** `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`), same token as sg-recipe-bot / nas-doctor. No API key.

**Self-healing layers:** in-app retry → in-app `claude -p` repair (data/state only) → Docker healthcheck restart → NAS Doctor (`claude -p` with docker access, alerts topic 2930).

See [[Deploy]].
