#!/bin/sh
# Copy the committed code to the NAS and rebuild the container.
# data/ (your real balances and watchlist) is only copied on the first deploy, never overwritten.
set -e
NAS="${NAS:-James Koh@192.168.1.27}"
DIR=/volume1/docker/miles-chase
git archive HEAD | ssh "$NAS" "mkdir -p $DIR && cd $DIR && tar xf - \$(test -d data && echo --exclude=data) && docker compose up -d --build"
