#!/usr/bin/env python3
"""Run the KrisFlyer miles prompts through Claude Code and post the result to Telegram.

Usage:
    python run_miles.py daily      # daily fare tracker
    python run_miles.py monthly    # monthly miles coach report
    python run_miles.py reminders  # expiry reminders, no Claude needed
    python run_miles.py news       # new blog posts / videos; Claude judges what matters
    python run_miles.py promos     # new KrisFlyer partner / KrisShop brand promos
    python run_miles.py ask "Transfer CR now for Tokyo?"   # one-off question to Claude
    python run_miles.py tickets    # award tickets your miles can book (Claude)

Options:
    --dry-run   print the assembled prompt and stop (no Claude call, nothing saved)
    --no-send   run Claude and save state, but print instead of posting to Telegram
    --full      daily only: post the full report even on a quiet day

Environment (or a .env file next to this script, see .env.example):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   where to post (if unset, the report is printed);
                                           use -100123/2765 to post in a group topic
    CLAUDE_MODEL                           optional model override for `claude -p`
    CLAUDE_BIN                             path to `claude` if it isn't on PATH (e.g. under cron)
    SPLIT_MESSAGES                         "sections" (default) posts each report section as its
                                           own message, only the first one notifying; "off" posts
                                           one message
    DAILY_QUIET                            quiet days: "line" (default) posts one short line,
                                           "silent" posts nothing, "off" posts the full report
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vault

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = ROOT / "state"

PROMPTS = {"daily": ROOT / "miles_daily.txt", "monthly": ROOT / "miles_monthly.txt"}
MY_POINTS = DATA / "my_points.txt"
WATCHLIST = DATA / "watchlist.txt"
FARE_DATA = DATA / "fare_data.txt"
MONTHLY_HISTORY = STATE / "monthly_history.txt"
PRICES_HISTORY = STATE / "prices.txt"

SEPARATOR = "=============================="
HISTORY_MONTHS_KEPT = 24
PRICES_DAYS_KEPT = 400
TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message
TELEGRAM_RETRIES = 3  # times to retry after "429 Too Many Requests"
LENGTH_TARGETS = {"daily": 3500, "monthly": 3500, "ask": 2500, "tickets": 14000}  # the limits the prompts ask for
CLAUDE_TIMEOUT_SECONDS = 20 * 60

SGT = timezone(timedelta(hours=8))
# Claude sometimes copies the brackets from the prompt's format example: PRICES [2026-09-30] | ...
PRICES_LINE = re.compile(r"^PRICES \[?(\d{4}-\d{2}-\d{2})\]?(?=\s|$).*$")

# Card balances to KrisFlyer miles, counted before transfer at the straight rate:
# (balance, miles), so 52,000 Citi Rewards points count as 20,800 miles.
CONVERSIONS = {
    "CR": (25000, 10000),   # Citi Rewards: ThankYou points
    "SCR": (25000, 10000),  # Standard Chartered Rewards: 360° Rewards points
    "CPM": (1, 1),          # Citi PremierMiles: already miles (Citi Miles), 1:1
    "KF": (1, 1),           # already in KrisFlyer
}
# What each card's balance is called.
UNITS = {"CR": "points", "SCR": "points", "CPM": "Citi Miles", "KF": "KrisFlyer miles"}
# Fee in S$ for moving a card's points to KrisFlyer in one transfer, from the prompts'
# baseline facts. Update these when a report lists them under "Baseline changes".
TRANSFER_FEES = {"CR": 27.25, "CPM": 27.25, "SCR": 27.00}
APPROX_FEES = {"SCR"}  # SC's fee is "about S$27 per rewards code"
BALANCE_LINE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*([\d,]+)")

# Card numbers (groups of 4) and long digit runs such as KrisFlyer or account numbers.
ACCOUNT_NUMBER = re.compile(r"\b(?:\d{4}[ -]){3}\d{1,7}\b|\b\d{10,19}\b")
URL = re.compile(r"https?://\S+")

# The daily prompt starts its output with this, so quiet days can be posted as one line.
STATUS_LINE = re.compile(r"^\s*STATUS:\s*(NEWS|QUIET)\s*$", re.IGNORECASE)
QUIET_MODES = ("line", "silent", "off")
SPLIT_MODES = ("sections", "off")
SHORT_SECTION = 200  # sections up to this long share a message with neighbouring short ones
GROUP_LIMIT = 900    # longest message made by grouping short sections
MEMORY_HEADER = ("MEMORY (from the app's vault, newest first: what was already sent and learned. "
                 "Don't repeat a tip or re-announce news listed here; say what changed instead)")
USER_TEXT_LIMIT = 80  # longest route or goal accepted from Telegram
ASK_LIMIT = 500       # longest /ask question


def load_env_file(path=ROOT / ".env"):
    """Set KEY=value lines from .env, without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def fail(message):
    sys.exit(f"error: {message}")


def read_lines(path):
    """Non-empty, non-comment lines of a file, or [] if it doesn't exist."""
    if not path.exists():
        return []
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def check_filled_in(path, lines):
    """Stop if a data file still has template placeholders like [points]."""
    if not lines:
        fail(f"{path.relative_to(ROOT)} is empty. Fill it in first (see README.md).")
    for line in lines:
        if re.search(r"\[[^\]]*\]", line):
            fail(f"{path.relative_to(ROOT)} still has a placeholder: {line!r}")


def parse_balances(lines):
    """{card: points} from lines like 'CR: 50,000 exp 2027-01'."""
    balances = {}
    for line in lines:
        match = BALANCE_LINE.match(line)
        if match and match.group(1).upper() in CONVERSIONS:
            balances[match.group(1).upper()] = int(match.group(2).replace(",", ""))
    return balances


def card_miles(card, points):
    """KrisFlyer miles a card's points are worth before transfer."""
    per_points, miles = CONVERSIONS[card]
    return points * miles // per_points


def miles_available(balances):
    """Total KrisFlyer miles if every card's points were converted."""
    return sum(card_miles(card, points) for card, points in balances.items())


def transfer_fee(card, points):
    """S$ to transfer a card's points in one go; nothing for KrisFlyer or an empty card."""
    return TRANSFER_FEES.get(card, 0) if points > 0 else 0


def miles_summary(balances):
    """Per-card miles and transfer fees plus totals, for the prompt and /points."""
    lines = []
    for card, points in balances.items():
        if card == "KF":
            lines.append(f"KF: {points:,} miles already in KrisFlyer")
            continue
        line = f"{card}: {points:,} {UNITS[card]} = {card_miles(card, points):,} KrisFlyer miles"
        fee = transfer_fee(card, points)
        if fee:
            line += f", transfer fee {'about ' if card in APPROX_FEES else ''}S${fee:,.2f}"
        lines.append(line)
    total_fee = sum(transfer_fee(card, points) for card, points in balances.items())
    approx = any(card in APPROX_FEES and points > 0 for card, points in balances.items())
    return lines + [
        f"Total: {miles_available(balances):,} miles",
        f"Transfer fees to pay: {'about ' if approx else ''}S${total_fee:,.2f} (one transfer per card)",
    ]


def set_balance(card, points, expiry=None):
    """Update one card's line in data/my_points.txt, keeping its expiry unless a new one is given."""
    card = card.upper()
    if card not in CONVERSIONS:
        raise ValueError(f"unknown card {card}, use one of {', '.join(CONVERSIONS)}")
    lines = MY_POINTS.read_text(encoding="utf-8").splitlines() if MY_POINTS.exists() else []
    for i, line in enumerate(lines):
        match = re.match(rf"^\s*{card}\s*:(.*)$", line, re.IGNORECASE)
        if match:
            old = re.search(r"\bexp\s+(\S+)", match.group(1))
            if not expiry and old and "[" not in old.group(1):
                expiry = old.group(1)
            lines[i] = f"{card}: {points}" + (f" exp {expiry}" if expiry else "")
            break
    else:
        lines.append(f"{card}: {points}" + (f" exp {expiry}" if expiry else ""))
    write_lines(MY_POINTS, lines)
    note = card_note(card, points, expiry, f"balance set to {points:,} via /points")
    vault.log("💰", "Points updated", f"{card} {points:,}" + (f" exp {expiry}" if expiry else ""), note)


def card_note(card, points, expiry, history):
    """Update the card's vault note (balance, expiry, History); returns its wikilink or None."""
    import reminders  # names live there; imported late since reminders imports this module
    if card == "KF":
        balance = f"{points:,} KrisFlyer miles"
    else:
        balance = f"{points:,} {UNITS[card]} = {card_miles(card, points):,} KrisFlyer miles"
    return vault.entity("Cards", reminders.NAMES.get(card, card),
                        f"**Balance:** {balance}\n**Expiry:** {expiry or 'none set'}", history)


def memory_lines(folders=()):
    """The vault memory block for a prompt, or nothing when the vault is off or empty."""
    text = vault.memory(folders)
    return [MEMORY_HEADER, text, ""] if text else []


def check_user_text(text, what, limit=USER_TEXT_LIMIT):
    """Validate a route or goal typed in Telegram before it goes into a data file."""
    if not text:
        raise ValueError(f"Give a {what}.")
    if len(text) > limit:
        raise ValueError(f"Keep the {what} under {limit} characters.")
    if "[" in text or "]" in text or text.lstrip().startswith("#"):
        raise ValueError(f"The {what} can't contain [ ] or start with #.")


def watchlist_routes():
    return [l for l in read_lines(WATCHLIST) if not l.upper().startswith("MY MILES:")]


def add_route(route):
    check_user_text(route, "route")
    if route.upper().startswith("MY MILES:"):
        raise ValueError("Routes can't start with MY MILES.")
    if route.lower() in (r.lower() for r in watchlist_routes()):
        raise ValueError(f"{route} is already on the watchlist.")
    lines = WATCHLIST.read_text(encoding="utf-8").splitlines() if WATCHLIST.exists() else []
    write_lines(WATCHLIST, lines + [route])


def remove_route(number):
    """Remove the route shown as `number` (1-based) in watchlist_routes(); returns it."""
    routes = watchlist_routes()
    if not 1 <= number <= len(routes):
        raise ValueError(f"Pick a number from 1 to {len(routes)}.")
    if len(routes) == 1:
        raise ValueError("That's the only route. Add another before removing it.")
    target, seen, kept = routes[number - 1], 0, []
    for line in WATCHLIST.read_text(encoding="utf-8").splitlines():
        if line.rstrip() == target:
            seen += 1
            if seen == 1:
                continue
        kept.append(line)
    write_lines(WATCHLIST, kept)
    return target


def get_goal():
    for line in read_lines(MY_POINTS):
        if re.match(r"^\s*goal\s*:", line, re.IGNORECASE):
            return line.split(":", 1)[1].strip()
    return None


def set_goal(goal):
    """Set the Goal line in data/my_points.txt, or remove it when goal is None."""
    if goal is not None:
        check_user_text(goal, "goal")
    lines = MY_POINTS.read_text(encoding="utf-8").splitlines() if MY_POINTS.exists() else []
    lines = [l for l in lines if not re.match(r"^\s*goal\s*:", l, re.IGNORECASE)]
    if goal is not None:
        lines.append(f"Goal: {goal}")
    write_lines(MY_POINTS, lines)


def split_status(report):
    """Return (report without its STATUS line, "NEWS"/"QUIET", or None if there was none)."""
    lines = report.strip().splitlines()
    if lines and (match := STATUS_LINE.match(lines[0])):
        return "\n".join(lines[1:]).strip(), match.group(1).upper()
    return report, None


def clean_report(report, mode):
    """Last safety net before posting: hide account-like numbers, strip markdown, check length."""
    parts = []
    last = 0
    for url in URL.finditer(report):  # leave links intact
        parts.append(ACCOUNT_NUMBER.sub("[number hidden]", report[last:url.start()]))
        parts.append(url.group())
        last = url.end()
    parts.append(ACCOUNT_NUMBER.sub("[number hidden]", report[last:]))
    report = "".join(parts)
    report = report.replace("**", "")
    report = re.sub(r"^#+\s*", "", report, flags=re.MULTILINE)
    return report


def shorten(report, mode):
    """If a report is over its length target, ask Claude once (no web search) to shorten it."""
    target = LENGTH_TARGETS[mode]
    if len(report) <= target:
        return report
    print(f"{mode} report is {len(report)} characters, over {target}; asking Claude to shorten it",
          file=sys.stderr)
    shorter = clean_report(run_claude(
        f"Shorten this Telegram report to under {target - 200} characters. Keep every emoji section "
        "header, number, date, URL and warning, and the same plain text style with no markdown. "
        "Cut repetition and explanations first. Print only the shortened report.\n\n" + report,
        tools=(),
    ), mode)
    if len(shorter) > target:
        print(f"warning: still {len(shorter)} characters; posting it in parts", file=sys.stderr)
    return shorter if len(shorter) < len(report) else report


def instructions(mode):
    """The instruction part of a prompt file, i.e. everything above the separator."""
    text = PROMPTS[mode].read_text(encoding="utf-8")
    if SEPARATOR not in text:
        fail(f"{PROMPTS[mode].name} has no '{SEPARATOR}' separator line")
    return text.split(SEPARATOR, 1)[0].rstrip()


def build_monthly(today):
    points = read_lines(MY_POINTS)
    check_filled_in(MY_POINTS, points)
    history = read_lines(MONTHLY_HISTORY)
    this_month = today.strftime("%Y-%m")
    # On a re-run within the same month, don't compare the month with itself.
    history = [h for h in history if not h.startswith(this_month)]

    prompt = "\n".join([
        instructions("monthly"),
        "",
        SEPARATOR,
        f"TODAY: {today.isoformat()}",
        "",
        "MY POINTS THIS MONTH",
        *points,
        "",
        "MILES AND TRANSFER FEES IF ALL CONVERTED (calculated by the script, before transfer)",
        *miles_summary(parse_balances(points)),
        "",
        "HISTORY (previous months, oldest first, filled by the script)",
        *history,
        "",
        *memory_lines(("Cards", "Deals")),
    ])
    return prompt, points


def build_daily(today):
    lines = read_lines(WATCHLIST)
    check_filled_in(WATCHLIST, lines)
    override = [l for l in lines if l.upper().startswith("MY MILES:")]
    routes = [l for l in lines if not l.upper().startswith("MY MILES:")]
    if not routes:
        fail("data/watchlist.txt has no routes")
    if override:
        miles = override[-1]
    else:
        points = read_lines(MY_POINTS)
        check_filled_in(MY_POINTS, points)
        miles = f"MY MILES: {miles_available(parse_balances(points))}"

    fare_data = read_lines(FARE_DATA)
    # YESTERDAY is the latest saved PRICES line from before today, so a re-run
    # later the same day still compares against yesterday.
    earlier = [
        l for l in read_lines(PRICES_HISTORY)
        if (m := PRICES_LINE.match(l)) and m.group(1) < today.isoformat()
    ]
    yesterday = earlier[-1:] if earlier else []

    return "\n".join([
        instructions("daily"),
        "",
        SEPARATOR,
        f"TODAY: {today.isoformat()}",
        "",
        "WATCHLIST",
        *routes,
        "",
        miles,
        "",
        "REGIONS (check for new deals, in this order; filled by the script)",
        *(f"{region}: {cities}" for region, cities in TICKET_DESTINATIONS.items()),
        "",
        "FARE_DATA (optional, filled by the script from a flight price API)",
        *fare_data,
        "",
        "YESTERDAY (filled by the script)",
        *yesterday,
        "",
        *memory_lines(("Deals",)),
    ])


ASK_PROMPT = """You are a KrisFlyer miles coach for a Singapore-based user. Answer their question
using their data below and, when it needs current facts (award space, transfer bonuses, card
promos, fees), a quick web search. Plain text for Telegram, no markdown, under {limit} characters.
No title line (the script adds one); a blank line between paragraphs; links as bare URLs.
Make it easy to scan with emoji: a flag before each country or region, ✈️ for flights and routes,
💺 for Business and Economy lines, ✅ / ❌ for whether their miles cover it, 💡 for tips.
Say which numbers are from their data and which you looked up; link the sources you used. If you
aren't sure, say so rather than guess. Never ask for or repeat card or account numbers."""


# The /tickets list (also posted after every /points update). Fixed layout: miles header, then one
# section per region with Economy and Business side by side, so sections() splits it by region.
BOOK_URL = "https://www.singaporeair.com/en_UK/sg/home"
# Every destination on singaporeair.com "Where we fly" (Singapore Airlines and Scoot, not partner
# airlines), read 2026-09-30. China first: the user's focus, with Xiamen a likely trip next year.
# "(Scoot)" = Scoot only. Re-check the page now and then; routes change.
TICKET_DESTINATIONS = {
    "🇨🇳 China": "Xiamen, Shanghai, Beijing, Guangzhou, Shenzhen, Chengdu, Chongqing, Hangzhou, "
                "Guiyang, Hong Kong, Fuzhou (Scoot), Haikou (Scoot), Macau (Scoot), Nanjing (Scoot), "
                "Nanning (Scoot), Qingdao (Scoot), Shantou (Scoot), Shenyang (Scoot), "
                "Tianjin (Scoot), Wuhan (Scoot), Xi'an (Scoot), Zhengzhou (Scoot)",
    "🇯🇵 Japan": "Tokyo, Osaka, Nagoya, Fukuoka, Sapporo (Scoot), Okinawa (Scoot)",
    "🇰🇷 South Korea": "Seoul, Busan, Jeju (Scoot)",
    "🇹🇼 Taiwan": "Taipei",
    "🌴 Southeast Asia": "Kuala Lumpur, Penang, Bangkok, Phuket, Chiang Mai, Bali, Jakarta, Surabaya, "
                        "Medan, Manila, Cebu, Ho Chi Minh City, Hanoi, Da Nang, Phnom Penh, Siem Reap, "
                        "Yangon, Bandar Seri Begawan, Krabi (Scoot), Langkawi (Scoot), "
                        "Kota Kinabalu (Scoot), Kuching (Scoot), Ipoh (Scoot), Malacca (Scoot), "
                        "Kuantan (Scoot), Kota Bharu (Scoot), Miri (Scoot), Sibu (Scoot), "
                        "Bintulu (Scoot), Hat Yai (Scoot), Chiang Rai (Scoot), Phu Quoc (Scoot), "
                        "Nha Trang (Scoot), Vientiane (Scoot), Lombok (Scoot), Labuan Bajo (Scoot), "
                        "Yogyakarta (Scoot), Makassar (Scoot), Manado (Scoot), Balikpapan (Scoot), "
                        "Padang (Scoot), Palembang (Scoot), Pekanbaru (Scoot), Pontianak (Scoot), "
                        "Semarang (Scoot), Tanjung Pandan (Scoot), Majalengka (Scoot), "
                        "Clark/Angeles (Scoot), Boracay/Caticlan (Scoot), Davao (Scoot), Iloilo (Scoot)",
    "🇮🇳 South Asia": "Male (Maldives), Colombo, Kathmandu, Dhaka, Delhi, Mumbai, Bengaluru, Chennai, "
                     "Hyderabad, Kolkata, Kochi, Ahmedabad, Amritsar (Scoot), Coimbatore (Scoot), "
                     "Thiruvananthapuram (Scoot), Tiruchirappalli (Scoot), Visakhapatnam (Scoot)",
    "🇦🇺 Australia & NZ": "Perth, Darwin, Adelaide, Melbourne, Sydney, Brisbane, Cairns, Auckland, "
                         "Christchurch",
    "🕌 Middle East & Africa": "Dubai, Riyadh, Jeddah (Scoot), Istanbul, Johannesburg, Cape Town",
    "🇪🇺 Europe": "London, Manchester, Paris, Frankfurt, Munich, Amsterdam, Brussels, Zurich, "
                 "Copenhagen, Milan, Rome, Barcelona, Madrid, Athens (Scoot)",
    "🇺🇸 USA": "Los Angeles, San Francisco, Seattle, New York",
}
TICKETS_PROMPT = f"""List the KrisFlyer Saver award tickets from Singapore the user can book now.
Count only miles they can actually move: KrisFlyer miles, plus card points in whole transfer blocks
at or above each bank's minimum (check it; Citi and Standard Chartered use blocks). Look up current
Saver prices (the 2026 chart); take your time and use up to 15 web searches. Follow this layout
exactly: plain text, no markdown, no tables, a blank line between blocks. It is read on a phone:
keep every line under 34 characters so nothing wraps. Fare prices use k (46k, 84.5k); the 💰
block uses full numbers with commas. Start directly with the 💰 line: no introduction before it.

💰 ESTIMATED MILES
Total if everything converted: 107,959
✅ Usable now: 100,000 (CR 250,000 points in 10 blocks)
⏳ Not yet: SCR 13,651 points (below the 25,000 minimum), CPM 4,983 (below the minimum)
(Numbers above are an example. Work them out from the data: the total is the script's figure,
usable counts only whole transfer blocks at or above each bank's minimum. Every ✅ and
"short" below uses the usable figure.)

Saver miles: one way · return
✅ can book · ❌17k = 17k short

🇯🇵 JAPAN

Tokyo, Osaka, Nagoya, Fukuoka
🛫 SIA direct · ~7h · daily
🪑 Eco 25.5k ✅ · 51k ✅
✨ PE 38k ✅ · 76k ✅
💼 Biz 54.5k ✅ · 109k ❌9k
👑 First 80k ❌60k (Tokyo)
💵 Tax ~S$70 one way
📈 Advantage: Eco 38k · Biz 85k

Sapporo, Okinawa (Scoot)
🛫 Scoot direct · ~7h
🪑 Eco 25.5k ✅ · 51k ✅
💵 Tax ~S$60 one way

(One section per region, in EXACTLY this order, China first, headed by its flag and name in
capitals:
{chr(10).join(f"{region}: {cities}" for region, cities in TICKET_DESTINATIONS.items())}
Within a region, put cities that cost the same miles in one block, cheapest block first. Each
block has the city names, then these short lines as in the example: 🛫 direct or via where,
flight time and how often; one line per cabin actually sold on the route (Economy, Premium Economy,
Business, First, in that order) so the cabins compare side by side; 💵 estimated taxes; and
📈 one way Advantage prices (Eco and Biz only) for when Saver is sold out. Always give the 🛫 and 💵 lines: flight times,
direct or via, and typical taxes are well known, so give them as "about" figures from what you
know. For award prices, use what you found and say "about" or "unconfirmed" when unsure; leave
out only a price you have no basis for. The numbers in the example are placeholders. When the user can't afford a fare, write ❌ plus the miles missing (❌9k),
never just "not enough"; if both one way and return are out of reach, give only the one way
shortfall to keep the line short. Scoot-only cities keep "(Scoot)" and get the Economy line only, since
Scoot has no business class. Leave out any city with no KrisFlyer award.)

🎯 Closest next
• Tokyo Biz return · 9k short
• (at most 2 more, same short form)

💡 Before you book
• Find the seat first, then transfer
• (at most 2 more tips, one short line each)

🔗 Book: {BOOK_URL} → Book Trip → Redeem flights
📚 Prices: (one source link)"""


def build_ask(today, question, mode="ask"):
    if mode == "tickets":
        question = TICKETS_PROMPT
    else:
        check_user_text(question, "question", limit=ASK_LIMIT)
    points = read_lines(MY_POINTS)
    return "\n".join([
        ASK_PROMPT.format(limit=LENGTH_TARGETS[mode]),
        "",
        SEPARATOR,
        f"TODAY: {today.isoformat()}",
        "",
        "MY POINTS",
        *points,
        "",
        "MILES AND TRANSFER FEES IF ALL CONVERTED (calculated by the script)",
        *miles_summary(parse_balances(points)),
        "",
        "WATCHLIST",
        *watchlist_routes(),
        "",
        *(memory_lines(("Cards",)) if mode == "ask" else []),
        "QUESTION",
        question,
    ])


def run_claude(prompt, tools=("WebSearch", "WebFetch"), model=None):
    cmd = [os.environ.get("CLAUDE_BIN") or "claude", "-p"]
    if tools:
        cmd += ["--allowedTools", *tools]
    model = model or os.environ.get("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        fail("the `claude` command isn't installed (npm install -g @anthropic-ai/claude-code)")
    except subprocess.TimeoutExpired:
        fail(f"Claude didn't finish within {CLAUDE_TIMEOUT_SECONDS // 60} minutes")
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        sys.stderr.write(result.stderr)
        fail(f"Claude exited with code {result.returncode} and output {output[:500]!r}")
    return output


def split_prices_line(report):
    """Return (report without its PRICES line, the PRICES line or None)."""
    lines = report.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if PRICES_LINE.match(lines[i].strip()):
            prices = lines[i].strip()
            return "\n".join(lines[:i] + lines[i + 1:]).rstrip(), prices
    return report, None


def save_prices(prices, today):
    if not prices:
        print("warning: no PRICES line in the output, so tomorrow has nothing to compare to",
              file=sys.stderr)
        return
    prices = re.sub(r"^PRICES \[(\d{4}-\d{2}-\d{2})\]", r"PRICES \1", prices)
    date = PRICES_LINE.match(prices).group(1)
    if date != today.isoformat():
        print(f"warning: PRICES line is dated {date}, expected {today}; saving it under today",
              file=sys.stderr)
        prices = prices.replace(date, today.isoformat(), 1)
    kept = [l for l in read_lines(PRICES_HISTORY) if not l.startswith(f"PRICES {today}")]
    write_lines(PRICES_HISTORY, (kept + [prices])[-PRICES_DAYS_KEPT:])


def save_monthly_history(points, today):
    month = today.strftime("%Y-%m")
    entry = f"{month}: " + "; ".join(points)
    kept = [l for l in read_lines(MONTHLY_HISTORY) if not l.startswith(month)]
    write_lines(MONTHLY_HISTORY, (kept + [entry])[-HISTORY_MONTHS_KEPT:])


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def chunks(text, limit=TELEGRAM_LIMIT):
    """Split text on line breaks into pieces Telegram will accept."""
    pieces, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:  # a single overlong line
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def telegram_api(token, method, payload, timeout=30, retries=TELEGRAM_RETRIES):
    """Call a Telegram Bot API method and return its result, or raise RuntimeError."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                reply = json.load(response)
            break
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            # Groups allow about 20 bot messages a minute; a long report can hit that. Wait as told.
            if error.code == 429 and attempt < retries:
                try:
                    wait = int(json.loads(body)["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError):
                    wait = 30
                time.sleep(min(wait, 120) + 1)
                continue
            raise RuntimeError(f"Telegram {method} failed: {body}")
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {reply}")
    return reply["result"]


def sections(report):
    """Split a report into messages, one per section (blocks separated by blank lines).

    A single-line block, such as a report title, is joined to the block after it. Consecutive
    short sections (up to SHORT_SECTION characters) share one message, up to GROUP_LIMIT, so a
    run of small sections like deals, tip and sources doesn't become a message each.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", report.strip()) if b.strip()]
    joined, carry = [], None
    for i, block in enumerate(blocks):
        if carry:
            block = f"{carry}\n\n{block}"
            carry = None
        if "\n" not in block and i < len(blocks) - 1:
            carry = block
            continue
        joined.append(block)
    if carry:
        joined.append(carry)

    messages, last_is_group = [], False
    for block in joined:
        short = len(block) <= SHORT_SECTION
        if short and last_is_group and len(messages[-1]) + 2 + len(block) <= GROUP_LIMIT:
            messages[-1] += "\n\n" + block
            continue
        messages.append(block)
        last_is_group = short
    return messages


def split_chat_id(chat_id):
    """"-100123/2765" (a group topic) -> ("-100123", 2765); a plain chat id -> (chat_id, None)."""
    chat, _, topic = str(chat_id).partition("/")
    return chat, int(topic) if topic else None


def send_typing(token, chat_id):
    chat, topic = split_chat_id(chat_id)
    payload = {"chat_id": chat, "action": "typing"}
    if topic:
        payload["message_thread_id"] = topic
    # Short and no retries: it runs in the heartbeat loop.
    telegram_api(token, "sendChatAction", payload, timeout=5, retries=0)


def send_telegram(text, token, chat_id, silent=False):
    """Post Telegram HTML (built with card() / llm_html()). The one send path for every message.

    Splits at TELEGRAM_LIMIT between blocks, never inside a tag. If Telegram rejects the HTML,
    the piece is resent as plain text so nothing is lost.
    """
    chat, topic = split_chat_id(chat_id)
    for i, piece in enumerate(html_chunks(text)):
        payload = {
            "chat_id": chat,
            "text": piece,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent or i > 0,
        }
        if topic:
            payload["message_thread_id"] = topic
        try:
            telegram_api(token, "sendMessage", payload)
        except RuntimeError as error:
            if "can't parse entities" not in str(error):
                raise
            plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
            for plain in chunks(html_to_plain(piece)):
                telegram_api(token, "sendMessage", {**plain_payload, "text": plain})


def send_report(report, token, chat_id, split, head=""):
    """Post a plain-text Claude report, one message per section when split; only the first notifies."""
    messages = report_html(report, split, head)
    for i, message in enumerate(messages):
        send_telegram(message, token, chat_id, silent=i > 0)
    return len(messages)


# ---- Telegram HTML cards (James's Telegram message style) ----
DIVIDER = "━━━━━━━━━━━━━━━━"
# One fixed emoji + title per message type.
SECTION_TITLES = {
    "daily": "✈️ MILES DAILY", "monthly": "📊 MILES MONTHLY", "tickets": "🎟 AWARD TICKETS",
    "ask": "💬 MILES ANSWER", "news": "🆕 MILES NEWS", "videos": "🎥 MILES VIDEOS", "promos": "🛍️ MILES PROMOS",
    "reminder": "⏰ EXPIRY REMINDER", "error": "⚠️ MILES ERROR", "points": "💰 BALANCES",
    "watch": "🗺 WATCHLIST", "goal": "🎯 GOAL", "help": "🧭 MILES CHASE", "note": "ℹ️ MILES CHASE",
}
MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
LINK_OR_URL = re.compile(rf"{MD_LINK.pattern}|{URL.pattern}")
BACKGROUND_BLOCK = re.compile(r"^\W*(sources?|baseline)\b|^📚", re.IGNORECASE)
TITLE_MAX = 48  # a block's first line up to this long (no URL, no full stop) is shown bold
HTML_TOKEN = re.compile(r"<[^>]*>|&#?\w+;|\n\n|.", re.DOTALL)
TAG_RESERVE = 300  # room for the tags closed/reopened around a split


def esc(text):
    return html.escape(str(text), quote=False)  # Telegram needs only < > & escaped in text


def link(url, label=None):
    """<a> with a short label (the site's name) instead of a long raw URL."""
    if label is None:
        label = re.sub(r"^www\.", "", url.split("://", 1)[-1].split("/", 1)[0])
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def header(kind, subtitle=""):
    emoji, title = SECTION_TITLES[kind].split(" ", 1)
    return f"{emoji} <b>{title}</b>" + (f" · {esc(subtitle)}" if subtitle else "")


def background(title, text):
    """Secondary detail for the end of a message: divider + expandable quote."""
    return f"{DIVIDER}\n<blockquote expandable>⚙️ <b>{esc(title)}</b>\n{esc(text)}</blockquote>"


def card(kind, subtitle, *blocks):
    """Header line, then the (already HTML) blocks, a blank line apart."""
    return "\n\n".join([header(kind, subtitle), *(b for b in blocks if b)])


def line_html(line):
    """One line of plain (or slightly markdown) text as safe HTML: escaped, links shortened."""
    line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
    out, last = [], 0
    for match in LINK_OR_URL.finditer(line):
        out.append(esc(line[last:match.start()]))
        if match.group(1):  # [label](url)
            out.append(link(match.group(2), match.group(1)))
            last = match.end()
        else:
            url = match.group().rstrip(".,;:!?)")
            out.append(link(url))
            last = match.start() + len(url)
    out.append(esc(line[last:]))
    return re.sub(r"\*\*([^*<>]+?)\*\*", r"<b>\1</b>", "".join(out))


def llm_html(text):
    """Claude's plain-text answer as Telegram HTML. Everything is escaped first, so only tags
    made here reach Telegram. A block's short first line is bold; Sources/Baseline blocks go
    into an expandable quote at the end."""
    blocks, extra = [], []
    for block in re.split(r"\n\s*\n", text.strip()):
        raw = block.strip().splitlines()
        if not raw:
            continue
        lines = [line_html(l) for l in raw]
        first = raw[0].strip()
        caps = first == first.upper() and any(c.isalpha() for c in first)  # e.g. "🇯🇵 JAPAN"
        if ((len(raw) > 1 or caps) and len(first) <= TITLE_MAX and not URL.search(first)
                and not first.endswith((".", "!", "?"))):
            lead, rest = re.match(r"([^\w\s&<(\[]+\s+)?(.*)", lines[0]).groups()
            lines[0] = f"{lead or ''}<b>{re.sub(r'</?b>', '', rest)}</b>"
        (extra if BACKGROUND_BLOCK.search(first) else blocks).append("\n".join(lines))
    if extra:
        blocks.append(f"{DIVIDER}\n<blockquote expandable>" + "\n\n".join(extra) + "</blockquote>")
    return "\n\n".join(blocks)


def report_html(report, split, head=""):
    """A Claude report as HTML messages (one per section when split), header card on the first."""
    messages = [llm_html(m) for m in (sections(report) if split else [report])]
    if head:
        messages[0] = f"{head}\n\n{messages[0]}"
    return messages


def html_to_plain(text):
    """Telegram HTML back to plain text for the parse-error fallback; links keep their URL."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2: \1", text, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def tag_name(tag):
    return re.match(r"<(\w[\w-]*)", tag).group(1)


def html_chunks(text, limit=TELEGRAM_LIMIT):
    """Split HTML into pieces of at most `limit`, never inside a tag or an entity.

    Prefers a blank line outside any tag, then a line break, then a space. A cut inside
    <b>/<blockquote>/<a> closes those tags and reopens them in the next piece.
    """
    pieces = []
    while len(text) > limit:
        stack, best, pos = [], None, 0
        for token in HTML_TOKEN.findall(text):
            pos += len(token)
            if pos > limit - TAG_RESERVE:
                break
            if token.startswith("</"):
                if stack:
                    stack.pop()
            elif token.startswith("<") and len(token) > 1:
                stack.append(token)
            else:
                kind = {"\n\n": 3, "\n": 2, " ": 1}.get(token, 0)
                # A nice break in the first half would make a tiny piece: rank it lowest.
                rank = kind * 2 + (not stack) if pos >= (limit - TAG_RESERVE) // 2 else -1
                if best is None or rank >= best[0]:
                    best = (rank, pos, list(stack))
        if best is None:  # only tags before the limit: not produced by this app
            best = (0, limit - TAG_RESERVE, [])
        _, cut, open_tags = best
        closing = "".join(f"</{tag_name(t)}>" for t in reversed(open_tags))
        piece = text[:cut].rstrip() + closing
        if piece.strip():
            pieces.append(piece)
        text = "".join(open_tags) + text[cut:].lstrip("\n ")
    if text.strip():
        pieces.append(text)
    return pieces


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=sorted([*PROMPTS, "reminders", "news", "promos", "ask", "tickets"]))
    parser.add_argument("question", nargs="*", help="ask only: the question for Claude")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    load_env_file()

    today = datetime.now(SGT).date()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    posting = not (args.no_send or args.dry_run) and token and chat_id
    quiet = os.environ.get("DAILY_QUIET", "line").strip().lower()
    if quiet not in QUIET_MODES:
        fail(f"DAILY_QUIET must be one of {', '.join(QUIET_MODES)}, not {quiet!r}")
    split = os.environ.get("SPLIT_MESSAGES", "sections").strip().lower()
    if split not in SPLIT_MODES:
        fail(f"SPLIT_MESSAGES must be one of {', '.join(SPLIT_MODES)}, not {split!r}")

    if args.mode in ("news", "promos"):
        import news_watch
        import promo_watch
        watch = news_watch if args.mode == "news" else promo_watch
        send = (lambda text: send_telegram(text, token, chat_id)) if posting else print
        try:
            count, errors = watch.check(send, save=bool(posting))
        except (RuntimeError, OSError) as error:
            fail(str(error))
        for error in errors:
            print(f"warning: couldn't read {error}", file=sys.stderr)
        print(f"Found {count} new {'post' if args.mode == 'news' else 'promo'}(s).")
        return

    if args.mode == "reminders":
        import reminders
        if not posting:  # print only, and don't mark anything as sent
            for _, text in reminders.due(today, read_lines(MY_POINTS), reminders.load_sent()):
                print(text + "\n")
            return
        try:
            count = reminders.run(today, lambda text: send_telegram(text, token, chat_id))
        except (RuntimeError, OSError) as error:
            fail(str(error))
        print(f"Sent {count} expiry reminder(s).")
        return

    if args.mode in ("ask", "tickets"):
        try:
            prompt = build_ask(today, " ".join(args.question).strip(), args.mode)
        except ValueError as error:
            fail(str(error))
        if args.dry_run:
            print(prompt)
            return
        answer = shorten(clean_report(run_claude(prompt), args.mode), args.mode)
        question = " ".join(args.question).strip()
        head = header(args.mode, f"{today:%d %b %Y}" if args.mode == "tickets"
                      else question if len(question) <= 40 else question[:39] + "…")
        # The ticket list is laid out in blocks, so it posts one message per section.
        by_section = args.mode == "tickets" and split == "sections"
        if not posting:
            print("\n\n────────── next message ──────────\n\n".join(report_html(answer, by_section, head)))
            return
        try:
            send_report(answer, token, chat_id, by_section, head)
        except (RuntimeError, OSError) as error:
            fail(str(error))
        if args.mode == "tickets":
            vault.log("🎟", "Tickets list sent", vault.gist(answer))
        else:
            vault.log("💬", "/miles answered", f"Q: {question} → A: {vault.gist(answer, 200)}")
        print(f"Posted the answer to Telegram ({len(answer)} characters).")
        return

    if args.mode == "monthly":
        prompt, points = build_monthly(today)
    else:
        prompt = build_daily(today)

    if args.dry_run:
        print(prompt)
        return

    report = clean_report(run_claude(prompt), args.mode)
    report, status = split_status(report)

    if args.mode == "daily":
        report, prices = split_prices_line(report)
        save_prices(prices, today)
    else:
        save_monthly_history(points, today)
        if posting:
            import reminders
            expiries = {c: f"{last:%Y-%m}" for c, _, last in reminders.expiries(points)}
            for c, balance in parse_balances(points).items():  # not `card`: that's the card() helper
                card_note(c, balance, expiries.get(c), f"monthly snapshot {balance:,}")

    head = header(args.mode, f"{today:%d %b %Y}")
    if args.mode == "daily" and status == "QUIET" and quiet != "off" and not args.full:
        if quiet == "silent":
            if posting:
                vault.log("✈️", "Daily report: quiet day", "nothing posted")
            print("Quiet day: nothing posted.")
            return
        report = "no fare changes or deals today"
        messages = [card("daily", f"{today:%d %b %Y}",
                         "⚪ No fare changes or deals on your watchlist today.\n"
                         "<i>Send /run daily for the full report.</i>")]
    else:
        report = shorten(report, args.mode)
        messages = report_html(report, split == "sections", head)

    if not posting:
        print("\n\n────────── next message ──────────\n\n".join(messages))
        return
    try:
        for i, message in enumerate(messages):
            send_telegram(message, token, chat_id, silent=i > 0)
        count = len(messages)
    except (RuntimeError, OSError) as error:
        fail(str(error))
    vault.log("📊" if args.mode == "monthly" else "✈️", f"{args.mode.capitalize()} report sent",
              f"{status or 'NEWS'}: {vault.gist(report)}")
    print(f"Posted the {args.mode} report to Telegram ({len(report)} characters, "
          f"{count} message{'s' if count != 1 else ''}).")


if __name__ == "__main__":
    main()
