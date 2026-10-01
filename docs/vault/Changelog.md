---
tags: [active]
updated: 2026-10-01
---
# Changelog

## 2026-10-01
- fix: `/tickets` layout fits a phone: lines under 34 chars, fares in k (46k), ✅ / ❌17k instead of "→ left 54,000 / ❌ 17,000 short", one way Advantage (Eco/Biz), 🎯 Closest next as short bullets (`TICKETS_PROMPT` in `run_miles.py`).
- fix: KF line in the `data/my_points.txt` template no longer asks for an expiry (`exp [YYYY-MM]`), since KrisFlyer has no single expiry to look up; the leftover placeholder was blocking the monthly report.

## 2026-09-30
- fix: `telegram_api` waits and retries on 429 Too Many Requests (group limit ~20 msgs/min hit by the long /tickets list); typing indicator uses `retries=0` so the heartbeat loop never sleeps.
- feat: `/tickets` detail per block: 🛫 route/flight time/frequency, all cabins sold (Economy, Premium Economy, Business, First), 💵 est. taxes, 📈 Advantage fallback prices. Length target 14,000.
- feat: `/tickets` layout v3: 💰 estimated miles header (total / usable / not yet), one section per region with Economy and Business side by side, "❌ N short" instead of "not enough".
- feat: daily report moved to 18:00 and adds REGION UPDATES (new deals per region in `TICKET_DESTINATIONS`, China first; only regions with news). Quiet days silent (`DAILY_QUIET=silent` in .env).
- feat: `/tickets` covers all 133 destinations on singaporeair.com "Where we fly" (SQ + Scoot-only, scraped 2026-09-30 via Chrome; Scoot-only in Economy only).
- feat: `/tickets` covers popular Asia by region, China first (Xiamen, Shanghai, Beijing, ...), Japan incl. Osaka/Sapporo, Korea, Taiwan, SE Asia, South Asia; same-price cities grouped per block (`TICKET_DESTINATIONS` in [run_miles.py](../../run_miles.py)).
- feat: "typing…" shown in the topic while Claude jobs run (`CLAUDE_MODES` in [scheduler.py](../../scheduler.py), re-sent every 5 s from the heartbeat loop).
- feat: `tickets` job / `/tickets` command: fixed, spaced layout (Business then Economy, one destination per block, one message per section), counts only transferable miles, verified SG booking link ([run_miles.py](../../run_miles.py) `TICKETS_PROMPT`).
- feat: new miles videos from YouTube channel feeds (MileLion, Suitesmile, Lets Get To The Points; HoneyMoneySG and Kelvin filtered to miles topics) posted as "🎥 New miles videos" ([news_watch.py](../../news_watch.py)).
- feat: after a `/points` update, Claude posts the Business and Economy Saver tickets the new total can book, plus the singaporeair.com booking link (one run per batch of updates, via the `ask` job).
- feat: `/ask <question>` — free-form `claude -p` answer with balances/watchlist as context ([run_miles.py](../../run_miles.py) `ask` mode, [telegram_bot.py](../../telegram_bot.py)).
- feat: `TELEGRAM_CHAT_ID` accepts `chat/topic` to post and listen in a group topic.
- fix: compose `pull_policy: build` (Dockge Update now rebuilds), runs as uid 1000, TZ set.
- feat: [deploy.sh](../../deploy.sh) — redeploys never overwrite `data/`.
- docs: vault created.
- ops: bot @jameskoh_miles_bot, topic 2988, deployed to NAS (healthy, claude -p verified in container).
