"""New-deal alerts from miles blogs, checked every 30 minutes. No Claude needed.

Reads the blogs' RSS feeds and sends one Telegram message listing any new post that mentions
KrisFlyer, Singapore Airlines, your cards or a city on your watchlist. Posts already seen are
remembered in state/news_seen.json. The first check only records what's there, so you don't get
a flood of old posts.
"""

import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

import run_miles

FEEDS = {
    "The MileLion": "https://milelion.com/feed/",
    "Mainly Miles": "https://mainlymiles.com/feed/",
}
SEEN = run_miles.STATE / "news_seen.json"
SEEN_KEPT = 500
MAX_ITEMS_PER_MESSAGE = 10
KEYWORDS = [
    "KrisFlyer", "Singapore Airlines", "SIA", "Scoot", "Spontaneous Escapes", "Saver",
    "Kris+", "Star Alliance", "award", "fare sale", "transfer bonus", "Citi", "PremierMiles",
    "Citi Rewards", "ThankYou", "Standard Chartered", "SC Rewards", "360 Rewards",
]
TAG = re.compile(r"<[^>]+>")


def keywords():
    """The standard keywords plus each watchlist city, e.g. "Tokyo" from "SIN Tokyo, Mar 2027"."""
    words = list(KEYWORDS)
    for route in run_miles.watchlist_routes():
        place = route.split(",")[0]
        words += [w for w in place.split() if w.upper() != "SIN" and len(w) > 2]
    return words


def matches(text, words):
    return [w for w in words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text, re.IGNORECASE)]


def fetch(url, timeout=20):
    """(title, link, summary) for each item in an RSS feed."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Miles Chase alerts)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        root = ET.fromstring(response.read())
    items = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = html.unescape(TAG.sub(" ", item.findtext("description") or ""))
        if title and link:
            items.append((title, link, " ".join(summary.split())[:400]))
    return items


def load_seen():
    """{feed name: seen links}. A feed missing here hasn't been read yet."""
    try:
        seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    except (ValueError, OSError):
        return {}
    return seen if isinstance(seen, dict) else {}


def check(send, fetch=fetch, save=True):
    """Send one message about new matching posts. Returns (posts found, feed errors).

    The first time a feed is read, its posts are only recorded, so old posts aren't sent.
    """
    seen = load_seen()
    words = keywords()
    found, errors = [], []
    for name, url in FEEDS.items():
        try:
            items = fetch(url)
        except (OSError, ET.ParseError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        first_read = name not in seen
        links = seen.setdefault(name, [])
        known = set(links)
        for title, link, summary in items:
            if link in known:
                continue
            known.add(link)
            links.append(link)
            if not first_read and matches(f"{title} {summary}", words):
                found.append((name, title, link))
        seen[name] = links[-SEEN_KEPT:]
    if found:
        lines = ["🆕 New from the miles blogs"]
        for name, title, link in found[:MAX_ITEMS_PER_MESSAGE]:
            lines += ["", title, f"{name} · {link}"]
        if len(found) > MAX_ITEMS_PER_MESSAGE:
            lines += ["", f"and {len(found) - MAX_ITEMS_PER_MESSAGE} more"]
        send("\n".join(lines))
    if save:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen, indent=1) + "\n")
    return len(found), errors
