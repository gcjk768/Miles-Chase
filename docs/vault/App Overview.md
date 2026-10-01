---
tags: [active]
updated: 2026-10-01
---
# App Overview

One container (`miles-chase`), one loop: [scheduler.py](../../scheduler.py) runs jobs on time (SGT) and long-polls Telegram between them.

| File | Role |
|---|---|
| [run_miles.py](../../run_miles.py) | Job runner: `daily`, `monthly` (Claude + web search), `ask` (Claude), `reminders`, `news` (no Claude). Telegram send helpers, `split_chat_id` for `chat/topic` ids. `TICKETS_PROMPT` fixes the phone-width /tickets layout (lines < 34 chars, fares in k); `ASK_PROMPT` asks for emoji-led answers. |
| [scheduler.py](../../scheduler.py) | Schedule (daily 18:00, monthly 1st 08:07, reminders 09:00, news every 30 min), retries (transient errors ×3), one self-repair a day per job, heartbeat. `/ask` failures skip retry/repair. |
| [telegram_bot.py](../../telegram_bot.py) | Commands `/points /watch /goal /run /miles /mileshelp` (`/ask` = alias); only reads its own chat (and topic, if set). A `/points` update (or `/tickets`) triggers one `tickets` job. |
| [repair.py](../../repair.py) | `claude -p` self-repair, may only edit `data/` and `state/`. |
| [healthcheck.py](../../healthcheck.py) | Docker healthcheck; kills a stuck scheduler so Docker restarts it. Unhealthy/restarting also trips NAS Doctor. |
| [news_watch.py](../../news_watch.py), [reminders.py](../../reminders.py) | Blog deal alerts + new YouTube miles videos (keyless channel RSS, `VIDEO_FEEDS` / `MIXED_VIDEO_FEEDS`), expiry reminders. |
| [miles_daily.txt](../../miles_daily.txt), [miles_monthly.txt](../../miles_monthly.txt) | Prompts. |
| [data/](../../data/) | User balances + watchlist (edited via Telegram). Never overwritten by [deploy.sh](../../deploy.sh) after first deploy. |
| [state/](../../state/) | History, offsets, heartbeat. |

**Claude sign-in:** `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`), same token as sg-recipe-bot / nas-doctor. No API key.

**Self-healing layers:** in-app retry → in-app `claude -p` repair (data/state only) → Docker healthcheck restart → NAS Doctor (`claude -p` with docker access, alerts topic 2930).

See [[Deploy]].
