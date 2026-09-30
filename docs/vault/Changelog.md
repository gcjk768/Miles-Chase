---
tags: [active]
updated: 2026-09-30
---
# Changelog

## 2026-09-30
- feat: `/ask <question>` — free-form `claude -p` answer with balances/watchlist as context ([run_miles.py](../../run_miles.py) `ask` mode, [telegram_bot.py](../../telegram_bot.py)).
- feat: `TELEGRAM_CHAT_ID` accepts `chat/topic` to post and listen in a group topic.
- fix: compose `pull_policy: build` (Dockge Update now rebuilds), runs as uid 1000, TZ set.
- feat: [deploy.sh](../../deploy.sh) — redeploys never overwrite `data/`.
- docs: vault created.
