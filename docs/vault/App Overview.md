---
tags: [active]
updated: 2026-10-02
---
# App Overview

One container (`miles-chase`), one loop: [scheduler.py](../../scheduler.py) runs jobs on time (SGT) and long-polls Telegram between them.

| File | Role |
|---|---|
| [run_miles.py](../../run_miles.py) | Job runner: `daily`, `monthly` (Claude + web search), `ask` (Claude), `reminders`, `news` (no Claude). Telegram send helpers, `split_chat_id` for `chat/topic` ids. **Telegram HTML cards:** `SECTION_TITLES` (one emoji + title per message type), `card`/`header`/`link`/`background`/`esc` build messages; `llm_html` escapes Claude text then adds bold titles and short links; `send_telegram` is the only send path (`parse_mode=HTML`, tag-safe `html_chunks`, plain-text fallback on a parse error). `TICKETS_PROMPT` fixes the phone-width /tickets layout (lines < 34 chars, fares in k); `ASK_PROMPT` asks for emoji-led answers. |
| [scheduler.py](../../scheduler.py) | Schedule (daily 18:00, monthly 1st 08:07, reminders 09:00, `news` + `promos` every hour 24/7), retries (transient errors ×3), one self-repair a day per job, heartbeat; calls `vault.migrate()` at start. `/ask` failures skip retry/repair. |
| [telegram_bot.py](../../telegram_bot.py) | Commands `/points /watch /goal /run /miles /mileshelp` (`/ask` = alias); only reads its own chat (and topic, if set). A `/points` update (or `/tickets`) triggers one `tickets` job. |
| [repair.py](../../repair.py) | `claude -p` self-repair, may only edit `data/` and `state/`. |
| [healthcheck.py](../../healthcheck.py) | Docker healthcheck; kills a stuck scheduler so Docker restarts it. Unhealthy/restarting also trips NAS Doctor. |
| [news_watch.py](../../news_watch.py) | Hourly: blog posts + YouTube videos (conditional GET, `state/news_feeds.json`), keyword prefilter, then the **Claude importance gate** `judge()` (opus → sonnet → unfiltered) only when something is new; 🆕 card with `💡 reason`. Dedup: `state/news_seen.json` + vault `alerted_links()`. |
| [promo_watch.py](../../promo_watch.py) | Hourly: KrisFlyer promotions page cards + KrisShop GraphQL discounts for `PROMO_BRANDS`; brand/bank/transfer promos sent directly, the rest via `news_watch.judge`. 🛍️ MILES PROMOS card. Dedup `state/promos_seen.json`. |
| [reminders.py](../../reminders.py) | Expiry reminders (09:00). |
| [vault.py](../../vault.py) | Obsidian vault (`VAULT_DIR`, `/vault` on the NAS): `Activity/YYYY/MM/YYYY-MM-DD.md` movement log, `Deals/` and `Cards/` entity notes with append-only `## History`, Home.md MOC (this month + latest), `memory()` capped 4000 chars newest-first for the prompts, `migrate()` for old flat notes. Best-effort, never raises. |
| [miles_daily.txt](../../miles_daily.txt), [miles_monthly.txt](../../miles_monthly.txt) | Prompts. |
| [data/](../../data/) | User balances + watchlist (edited via Telegram). Never overwritten by [deploy.sh](../../deploy.sh) after first deploy. |
| [state/](../../state/) | History, offsets, heartbeat, `news_seen.json` / `news_feeds.json` / `promos_seen.json` dedup. |

**Claude sign-in:** `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`), same token as sg-recipe-bot / nas-doctor. No API key.

**Self-healing layers:** in-app retry → in-app `claude -p` repair (data/state only) → Docker healthcheck restart → NAS Doctor (`claude -p` with docker access, alerts topic 2930).

See [[Deploy]].
