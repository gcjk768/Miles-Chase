"""Product and partner promos for KrisFlyer members, checked every hour with the news watch.

Sources (one request each, politely, no Claude unless something is new):
- The KrisFlyer promotions page on singaporeair.com: server-rendered cards (partner bonus-miles
  offers, bank sign-up bonuses, transfer bonuses). A card is new when its id + wording changes.
- KrisShop's product API (the Magento GraphQL endpoint its own site uses): one query with an
  alias per brand in PROMO_BRANDS (default Garmin, Dyson, Apple, Samsung, Sony, Bose, LG, Philips,
  Nintendo); a product counts when the brand is in its name and it's discounted. It's new when
  its sku + sale price hasn't been seen, so a deeper cut alerts again, the same price doesn't.
Kris+ merchant deals only exist inside the app (no public page), so they aren't watched.

Dedup: state/promos_seen.json maps each promo key to its title. The first read of a source only
records what's there. Promos that name one of the brands, a bank, a card or a transfer are sent
as they are; the rest (hotel stays and the like) go through the news gate (Claude haiku,
JUDGE_MODELS) which keeps only what matters to James. One 🆕 card per run, silent when nothing is new.
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.request

import news_watch
import run_miles
import vault

KF_PROMOS_URL = "https://www.singaporeair.com/en_UK/sg/plan-travel/promotions/kf-promotions/"
KRISSHOP_GRAPHQL = "https://kscommerce.krisshop.com/graphql"
KRISSHOP_PRODUCT = "https://www.krisshop.com/en/product/{sku}/{url_key}.html"
DEFAULT_BRANDS = "Garmin, Dyson, Apple, Samsung, Sony, Bose, LG, Philips, Nintendo"
SEEN = run_miles.STATE / "promos_seen.json"
SEEN_KEPT = 1000
MAX_ITEMS_PER_MESSAGE = 10
USER_AGENT = "Mozilla/5.0 (Miles Chase alerts)"
# Promos with one of these (or a brand) are sent without asking Claude.
SURE = ["credit card", "DBS", "UOB", "Citi", "HSBC", "Amex", "American Express", "Standard Chartered",
        "transfer bonus", "convert", "KrisShop", "Kris+", "sign up", "sign-up"]
CARD = re.compile(r'<div class="card" data-card-id="([^"]*)" data-popup="([^"]*)".*?<h6>(.*?)</h6>'
                  r'.*?<p class="description">(.*?)</p>', re.S)
TAG = re.compile(r"<[^>]+>")
PRODUCTS = ("products(search: %s, pageSize: 40) { items { name sku url_key calculated_miles_point "
            "special_to_date price_range { minimum_price { regular_price { value } final_price { value } "
            "discount { percent_off } } } } }")


def brands():
    raw = os.environ.get("PROMO_BRANDS", "").strip() or DEFAULT_BRANDS
    return [b.strip() for b in raw.split(",") if b.strip()]


def text(fragment):
    return " ".join(html.unescape(TAG.sub(" ", fragment)).split())


def get(url, data=None, timeout=25):
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_krisflyer(get=get):
    """Partner promo cards from the KrisFlyer promotions page."""
    page = get(KF_PROMOS_URL)
    return [{"source": "KrisFlyer", "id": card_id, "title": text(title), "detail": text(detail),
             "link": link, "until": ""}
            for card_id, link, title, detail in CARD.findall(page)]


def fetch_krisshop(get=get, brands=brands):
    """Discounted KrisShop products of the watched brands, one GraphQL request for all brands."""
    names = brands()
    query = "{ " + " ".join(f"b{i}: " + PRODUCTS % json.dumps(b) for i, b in enumerate(names)) + " }"
    data = json.loads(get(KRISSHOP_GRAPHQL, {"query": query}))
    if "data" not in data:
        raise ValueError(f"KrisShop answered {str(data)[:200]}")
    promos = []
    for i, brand in enumerate(names):
        for item in (data["data"].get(f"b{i}") or {}).get("items") or []:
            name = item.get("name") or ""
            price = item.get("price_range", {}).get("minimum_price", {})
            was, now = price.get("regular_price", {}).get("value"), price.get("final_price", {}).get("value")
            off = price.get("discount", {}).get("percent_off") or 0
            if not news_watch.matches(name, [brand]) or not (was and now and now < was and off > 0):
                continue
            detail = f"S${now:,.0f} (was S${was:,.0f}, ▼{off:.0f}%)"
            if item.get("calculated_miles_point"):
                detail += f" · {float(item['calculated_miles_point']):,.0f} miles"
            promos.append({"source": "KrisShop", "id": f"{item.get('sku')}:{now:.2f}",
                           "title": f"{brand} · {name.title()}", "detail": detail,
                           "link": KRISSHOP_PRODUCT.format(sku=item.get("sku"), url_key=item.get("url_key")),
                           "until": (item.get("special_to_date") or "")[:10]})
    return promos


SOURCES = {"KrisFlyer": fetch_krisflyer, "KrisShop": fetch_krisshop}


def key(promo):
    digest = hashlib.sha1(f"{promo['title']}|{promo['detail']}".encode()).hexdigest()[:10]
    return f"{promo['source']}:{promo['id']}:{digest}"


def sure(promo):
    """Mechanically interesting: names a watched brand, a bank or card, or a transfer."""
    return bool(news_watch.matches(f"{promo['title']} {promo['detail']}", brands() + SURE))


def check(send, sources=SOURCES, save=True, judge=news_watch.judge):
    """Send one card about new promos. Returns (promos alerted, source errors)."""
    seen = news_watch.load_json(SEEN)
    new, errors = [], []
    for source, fetch in sources.items():
        try:
            promos = fetch()
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{source}: {error}")
            continue
        first_read = not any(k.startswith(source + ":") for k in seen)
        for promo in promos:
            k = key(promo)
            if k in seen:
                continue
            seen[k] = promo["title"]
            if not first_read:
                new.append(promo)
    if save:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(dict(list(seen.items())[-SEEN_KEPT:]), indent=1) + "\n")
    if not new:
        return 0, errors

    kept = [p for p in new if sure(p)]
    unsure = [p for p in new if not sure(p)]
    if unsure:  # Claude decides the ambiguous ones (hotel stays, cruises, ...)
        verdicts = judge([(p["source"], p["title"], p["link"], p["detail"]) for p in unsure])
        for i, promo in enumerate(unsure, 1):
            if i in verdicts:
                kept.append({**promo, "why": verdicts[i]})
            elif save:
                vault.log("🚫", "Promo skipped by gate", f"{promo['link']} · {promo['title']}")
    if not kept:
        return 0, errors
    blocks = []
    for p in kept[:MAX_ITEMS_PER_MESSAGE]:
        lines = [f"🆕 {'🛍️' if p['source'] == 'KrisShop' else '✈️'} <b>{run_miles.esc(p['title'])}</b>",
                 f"{'💰' if p['source'] == 'KrisShop' else '🎁'} {run_miles.esc(p['detail'])}"]
        if p.get("until"):
            lines.append(f"⏰ until {run_miles.esc(p['until'])}")
        if p.get("why"):
            lines.append(f"💡 <i>{run_miles.esc(p['why'])}</i>")
        lines.append(f"🔗 {run_miles.link(p['link'], p['source'])}")
        blocks.append("\n".join(lines))
    if len(kept) > MAX_ITEMS_PER_MESSAGE:
        blocks.append(f"<i>and {len(kept) - MAX_ITEMS_PER_MESSAGE} more</i>")
    send(run_miles.card("promos", f"{len(kept)} new · KrisFlyer & KrisShop", *blocks))
    if save:
        for p in kept:
            note = vault.entity("Deals", p["title"], f"**Source:** {p['source']}\n**Offer:** {p['detail']}\n"
                                f"**Link:** {p['link']}" + (f"\n**Why:** {p['why']}" if p.get("why") else ""),
                                f"alerted in Telegram · {p['detail']}")
            vault.log("🛍️", "Promo alerted", f"{p['link']} · {p['title']} · {p['detail']}", note)
    return len(kept), errors
