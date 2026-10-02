"""New-deal alerts from miles blogs and new miles videos on YouTube, checked every 30 minutes.
No Claude needed.

Reads the blogs' RSS feeds and the YouTube channels' feeds, and sends one Telegram message listing
any new post that mentions KrisFlyer, Singapore Airlines, your cards or a city on your watchlist,
and any new video from a miles channel (general money channels only when it's about miles). Posts already seen are
remembered in state/news_seen.json, and a post the vault's Activity log shows was
already alerted is never sent again (so a lost state file doesn't repeat alerts). The first check only records what's there, so you don't get
a flood of old posts.
"""

import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

import run_miles
import vault

FEEDS = {
    "The MileLion": "https://milelion.com/feed/",
    "Mainly Miles": "https://mainlymiles.com/feed/",
}
YOUTUBE = "https://www.youtube.com/feeds/videos.xml?channel_id="
# Every video from these channels is about miles.
VIDEO_FEEDS = {
    "The MileLion (YouTube)": YOUTUBE + "UC0RTkb7cFoJObjaSZJikG9g",
    "Suitesmile (YouTube)": YOUTUBE + "UCx_t4hnsalB8LJ0XtNfY1nA",
    "Lets Get To The Points": YOUTUBE + "UCaE0KM4BEXBR1969_Urs6mw",
}
# Mostly investing, so only their videos that mention miles count.
MIXED_VIDEO_FEEDS = {
    "HoneyMoneySG": YOUTUBE + "UCTCSq-mUx0ZtGhJeb7sn92Q",
    "Kelvin Learns Investing": YOUTUBE + "UCaJh-OfqbuiVyewBTddcr3g",
}
VIDEO_KEYWORDS = ["miles", "mile", "points", "air miles", "business class", "lounge", "credit card"]
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
SEEN = run_miles.STATE / "news_seen.json"
SEEN_KEPT = 500
MAX_ITEMS_PER_MESSAGE = 10
KEYWORDS = [
    "KrisFlyer", "Singapore Airlines", "SIA", "Scoot", "Spontaneous Escapes", "Saver",
    "Kris+", "Star Alliance", "award", "fare sale", "transfer bonus", "Citi", "PremierMiles",
    "Citi Rewards", "ThankYou", "Standard Chartered", "SC Rewards", "360 Rewards",
    "KrisShop", "Garmin", "Dyson", "Apple", "iPhone", "MacBook", "iPad",
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
    """(title, link, summary) for each item in an RSS feed or entry in an Atom (YouTube) feed."""
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
    for entry in root.iter(ATOM + "entry"):
        title = (entry.findtext(ATOM + "title") or "").strip()
        link = entry.find(ATOM + "link")
        summary = entry.findtext(f"{MEDIA}group/{MEDIA}description") or ""
        if title and link is not None and link.get("href"):
            items.append((title, link.get("href"), " ".join(summary.split())[:400]))
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
    alerted = vault.alerted_links()
    words = keywords()
    found, errors = [], []
    for name, url in {**FEEDS, **VIDEO_FEEDS, **MIXED_VIDEO_FEEDS}.items():
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
            if first_read or link in alerted:
                continue
            if name in VIDEO_FEEDS or matches(
                    f"{title} {summary}", words + (VIDEO_KEYWORDS if name in MIXED_VIDEO_FEEDS else [])):
                found.append((name, title, link))
        seen[name] = links[-SEEN_KEPT:]
    posts = [f for f in found if f[0] in FEEDS]
    videos = [f for f in found if f[0] not in FEEDS]
    for kind, subtitle, emoji, items in (("news", "miles blogs", "📰", posts),
                                         ("videos", "YouTube", "▶️", videos)):
        if not items:
            continue
        blocks = [f"{emoji} <b>{run_miles.esc(title)}</b>\n🔗 {run_miles.link(link, name)}"
                  for name, title, link in items[:MAX_ITEMS_PER_MESSAGE]]
        if len(items) > MAX_ITEMS_PER_MESSAGE:
            blocks.append(f"<i>and {len(items) - MAX_ITEMS_PER_MESSAGE} more</i>")
        send(run_miles.card(kind, f"{len(items)} new · {subtitle}", *blocks))
        if save:
            for name, title, link in items:
                note = (vault.entity("Deals", title, f"**Source:** {name}\n**Link:** {link}",
                                     f"alerted in Telegram · {link}") if kind == "news" else None)
                vault.log(emoji, "Deal alerted" if kind == "news" else "Video alerted",
                          f"{link} · {name}: {title}", note)
    if save:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen, indent=1) + "\n")
    return len(found), errors
