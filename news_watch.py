"""New-deal alerts from miles blogs and new miles videos on YouTube, checked every hour, 24/7.

Reads the blogs' RSS feeds and the YouTube channels' feeds (conditional requests: a feed that
answers 304 Not Modified costs nothing), keeps the posts that mention KrisFlyer, Singapore
Airlines, your cards or a city on your watchlist, and any new video from a miles channel (general
money channels only when it's about miles). Only when there is something new, Claude (opus, then
sonnet; if both fail the items go out unfiltered) is asked which of them matter to James
(KrisFlyer earn/burn, DBS/UOB/Citi card promos, SQ award availability, transfer bonuses) and one
🆕 card is sent for those, with Claude's one-line reason. Silent otherwise. Posts already seen are
remembered in state/news_seen.json, and a post the vault's Activity log shows was
already alerted is never sent again (so a lost state file doesn't repeat alerts). The first check only records what's there, so you don't get
a flood of old posts.
"""

import html
import json
import re
import sys
import urllib.error
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
FEED_CACHE = run_miles.STATE / "news_feeds.json"  # {feed name: {"etag":…, "modified":…}}
SEEN_KEPT = 500
MAX_ITEMS_PER_MESSAGE = 10
KEYWORDS = [
    "KrisFlyer", "Singapore Airlines", "SIA", "Scoot", "Spontaneous Escapes", "Saver",
    "Kris+", "Star Alliance", "award", "fare sale", "transfer bonus", "Citi", "PremierMiles",
    "Citi Rewards", "ThankYou", "Standard Chartered", "SC Rewards", "360 Rewards",
    "DBS", "UOB", "Altitude", "PRVI", "KrisShop", "Garmin", "Dyson", "Apple", "iPhone", "MacBook", "iPad",
]
TAG = re.compile(r"<[^>]+>")
JUDGE_MODELS = ("opus", "sonnet")
JUDGE_PROMPT = """You screen miles news for James, a Singapore-based KrisFlyer collector who holds
Citi Rewards, Citi PremierMiles and Standard Chartered cards. He only wants to be interrupted for
things that are NEW and IMPORTANT to him:
- KrisFlyer earning or redeeming (bonus miles, devaluations, award chart or fee changes)
- DBS, UOB or Citi credit card promotions worth acting on (sign-up bonuses, bonus-miles campaigns)
- Singapore Airlines award availability (Spontaneous Escapes, Saver seats opening up, fare sales)
- Points transfer bonuses into KrisFlyer or from his cards' programmes
- Deals on Garmin, Dyson or Apple products paid with miles or earning bonus miles
Skip general travel writing, trip reports, aircraft news, other airlines' programmes he can't use,
and anything that only repeats an item already alerted.

Items (number, source, title, summary):
{items}

Reply with one line per item worth alerting, in the form `N: reason` (reason under 12 words,
no markdown). If nothing is worth alerting reply with the single word SKIP."""
VERDICT = re.compile(r"^\s*(\d+)\s*[:.)\-]\s*(.+?)\s*$", re.MULTILINE)


def keywords():
    """The standard keywords plus each watchlist city, e.g. "Tokyo" from "SIN Tokyo, Mar 2027"."""
    words = list(KEYWORDS)
    for route in run_miles.watchlist_routes():
        place = route.split(",")[0]
        words += [w for w in place.split() if w.upper() != "SIN" and len(w) > 2]
    return words


def matches(text, words):
    return [w for w in words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text, re.IGNORECASE)]


def fetch(url, timeout=20, cache=None):
    """(title, link, summary) for each item in an RSS feed or entry in an Atom (YouTube) feed.

    `cache` is this feed's dict of validators: when given, the request is conditional and the
    answer None means 304 Not Modified (nothing new); the dict is updated from the response."""
    headers = {"User-Agent": "Mozilla/5.0 (Miles Chase alerts)"}
    if cache:
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("modified"):
            headers["If-Modified-Since"] = cache["modified"]
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            info = getattr(response, "headers", None)
            if cache is not None and info:
                cache["etag"] = info.get("ETag") or ""
                cache["modified"] = info.get("Last-Modified") or ""
    except urllib.error.HTTPError as error:
        if error.code == 304 and cache is not None:
            return None
        raise
    root = ET.fromstring(body)
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


def load_json(path):
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_seen():
    """{feed name: seen links}. A feed missing here hasn't been read yet."""
    return load_json(SEEN)


def judge(items):
    """Ask Claude which new items matter: {index: reason}. Opus, then sonnet; on failure keep all."""
    listing = "\n".join(f"{i}. [{name}] {title} — {summary}" for i, (name, title, _, summary)
                        in enumerate(items, 1))
    for model in JUDGE_MODELS:
        try:
            answer = run_miles.run_claude(JUDGE_PROMPT.format(items=listing), tools=(), model=model)
        except (SystemExit, OSError) as error:  # run_claude exits on failure
            print(f"warning: news gate with {model} failed: {error}", file=sys.stderr)
            continue
        if answer.strip().upper().startswith("SKIP"):
            return {}
        verdicts = {int(n): why for n, why in VERDICT.findall(answer) if 1 <= int(n) <= len(items)}
        if verdicts:
            return verdicts
        print(f"warning: news gate with {model} answered oddly: {answer[:200]!r}", file=sys.stderr)
    return {i: "" for i in range(1, len(items) + 1)}  # unfiltered rather than lost


def check(send, fetch=fetch, save=True, judge=judge):
    """Send one message about new matching posts. Returns (posts found, feed errors).

    The first time a feed is read, its posts are only recorded, so old posts aren't sent.
    """
    seen = load_seen()
    cache = load_json(FEED_CACHE)
    alerted = vault.alerted_links()
    words = keywords()
    found, errors = [], []
    for name, url in {**FEEDS, **VIDEO_FEEDS, **MIXED_VIDEO_FEEDS}.items():
        try:
            items = fetch(url, cache=cache.setdefault(name, {}))
        except (OSError, ET.ParseError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        if items is None:  # 304 Not Modified
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
                found.append((name, title, link, summary))
        seen[name] = links[-SEEN_KEPT:]
    if save:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen, indent=1) + "\n")
        FEED_CACHE.write_text(json.dumps(cache, indent=1) + "\n")
    if not found:
        return 0, errors

    verdicts = judge(found)  # Claude is only asked when there is something new
    kept = [(name, title, link, verdicts[i]) for i, (name, title, link, _) in enumerate(found, 1)
            if i in verdicts]
    for i, (name, title, link, _) in enumerate(found, 1):
        if i not in verdicts and save:
            vault.log("🚫", "Skipped by gate", f"{link} · {name}: {title}")
    posts = [f for f in kept if f[0] in FEEDS]
    videos = [f for f in kept if f[0] not in FEEDS]
    for kind, subtitle, emoji, items in (("news", "miles blogs", "📰", posts),
                                         ("videos", "YouTube", "▶️", videos)):
        if not items:
            continue
        blocks = [f"🆕 {emoji} <b>{run_miles.esc(title)}</b>\n🔗 {run_miles.link(link, name)}"
                  + (f"\n💡 <i>{run_miles.esc(why)}</i>" if why else "")
                  for name, title, link, why in items[:MAX_ITEMS_PER_MESSAGE]]
        if len(items) > MAX_ITEMS_PER_MESSAGE:
            blocks.append(f"<i>and {len(items) - MAX_ITEMS_PER_MESSAGE} more</i>")
        send(run_miles.card(kind, f"{len(items)} new · {subtitle}", *blocks))
        if save:
            for name, title, link, why in items:
                note = (vault.entity("Deals", title, f"**Source:** {name}\n**Link:** {link}\n**Why:** {why}",
                                     f"alerted in Telegram · {link}") if kind == "news" else None)
                vault.log(emoji, "Deal alerted" if kind == "news" else "Video alerted",
                          f"{link} · {name}: {title}", note)
    return len(kept), errors
