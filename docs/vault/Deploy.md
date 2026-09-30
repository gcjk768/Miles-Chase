---
tags: [active]
updated: 2026-09-30
---
# Deploy (NAS)

Stack folder `/volume1/docker/miles-chase` (Dockge). From the repo: `./deploy.sh` = `git archive HEAD` → NAS, `docker compose up -d --build`. `data/` is copied only the first time.

`.env` on the NAS (chmod 600):
```
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=-1002069000031/2988   # James Channel topic "Miles-Chase"
CLAUDE_CODE_OAUTH_TOKEN=<same as sg-recipe-bot>
```
Bot must be a member of James Channel. Commands must be sent inside the Miles topic.

Bot privacy mode is on: in the group use `/points@jameskoh_miles_bot` (tap `/` for the menu), or disable privacy in BotFather and re-add the bot.

Container runs as uid 1000 (`user: "1000:10"`), so data/state stay editable from the host.

**NAS Doctor** needs no config: it watches every container via docker.sock; `miles-chase` going unhealthy/crash-looping gets an auto-fix attempt and an alert in topic 2930. It reads this vault / README before touching the stack.
