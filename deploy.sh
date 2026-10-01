#!/bin/sh
# Copy the committed code to the NAS and rebuild the container.
# data/ (your real balances and watchlist) is only copied on the first deploy, never overwritten.
set -e
NAS="${NAS:-James Koh@192.168.1.27}"
DIR=/volume1/docker/miles-chase
# The Obsidian vault folder is made here, as James (uid 1000): if Docker created it for the
# bind mount it would be owned by root and the app couldn't write to it.
VAULT="/volume1/James/Obsidian/Miles Chase"
git archive HEAD | ssh "$NAS" "mkdir -p '$VAULT' $DIR && cd $DIR && tar xf - \$(test -d data && echo --exclude=data) && docker compose up -d --build --force-recreate"  # code is bind-mounted: recreate so a code-only change is picked up
