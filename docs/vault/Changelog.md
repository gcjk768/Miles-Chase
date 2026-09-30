---
tags: [active]
updated: 2026-09-30
---
# Changelog

## 2026-09-30
- feat: after a `/points` update, Claude posts the Business and Economy Saver tickets the new total can book, plus the singaporeair.com booking link (one run per batch of updates, via the `ask` job).
- feat: `/ask <question>` — free-form `claude -p` answer with balances/watchlist as context ([run_miles.py](../../run_miles.py) `ask` mode, [telegram_bot.py](../../telegram_bot.py)).
- feat: `TELEGRAM_CHAT_ID` accepts `chat/topic` to post and listen in a group topic.
- fix: compose `pull_policy: build` (Dockge Update now rebuilds), runs as uid 1000, TZ set.
- feat: [deploy.sh](../../deploy.sh) — redeploys never overwrite `data/`.
- docs: vault created.
- ops: bot @jameskoh_miles_bot, topic 2988, deployed to NAS (healthy, claude -p verified in container).
