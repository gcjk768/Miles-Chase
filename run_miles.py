#!/usr/bin/env python3
"""Run the KrisFlyer miles prompts through Claude Code and post the result to Telegram.

Usage:
    python run_miles.py daily      # daily fare tracker
    python run_miles.py monthly    # monthly miles coach report
    python run_miles.py reminders  # expiry reminders, no Claude needed
    python run_miles.py news       # new posts on the miles blogs, no Claude needed
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
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    ])


ASK_PROMPT = """You are a KrisFlyer miles coach for a Singapore-based user. Answer their question
using their data below and, when it needs current facts (award space, transfer bonuses, card
promos, fees), a quick web search. Plain text for Telegram, no markdown, under {limit} characters.
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
exactly: plain text, no markdown, no tables, numbers with commas, a blank line between blocks.
Start directly with the 💰 line: no introduction before it.

💰 ESTIMATED MILES
Total if everything converted: 107,959
✅ Usable now: 100,000 (CR 250,000 points in 10 blocks)
⏳ Not yet: SCR 13,651 points (below the 25,000 minimum), CPM 4,983 (below the minimum)
(Numbers above are an example. Work them out from the data: the total is the script's figure,
usable counts only whole transfer blocks at or above each bank's minimum. Every "left" and
"short" below uses the usable figure.)

Saver prices · one way / return · miles left after

🇯🇵 JAPAN

Tokyo, Osaka, Nagoya, Fukuoka
🛫 Direct SIA, about 6h 30m to 7h 30m; Tokyo several flights a day
🪑 Economy 25,500 / 51,000 → left 74,500 / 49,000
✨ Premium Economy 38,000 / 76,000 → left 62,000 / 24,000
💼 Business 54,500 / 109,000 → left 45,500 / ❌ 9,000 short
👑 First 80,000 / 160,000 → ❌ 60,000 short (Tokyo only)
💵 Taxes about S$70 one way / S$140 return
📈 Advantage if Saver is gone: Economy 38,000, Business 85,000 one way

Sapporo, Okinawa (Scoot)
🛫 Direct Scoot, about 7h; Sapporo seasonal
🪑 Economy 25,500 / 51,000 → left 74,500 / 49,000
💵 Taxes about S$60 one way

(One section per region, in EXACTLY this order, China first, headed by its flag and name in
capitals:
{chr(10).join(f"{region}: {cities}" for region, cities in TICKET_DESTINATIONS.items())}
Within a region, put cities that cost the same miles in one block, cheapest block first. Each
block has the city names, then these lines as in the example: 🛫 direct or via where, flight
time and how often; one line per cabin actually sold on the route (Economy, Premium Economy,
Business, First, in that order) so the cabins compare side by side; 💵 estimated taxes; and
📈 Advantage prices for when Saver is sold out. Always give the 🛫 and 💵 lines: flight times,
direct or via, and typical taxes are well known, so give them as "about" figures from what you
know. For award prices, use what you found and say "about" or "unconfirmed" when unsure; leave
out only a price you have no basis for. The numbers in the example are placeholders. When the user can't afford a fare, write "❌ N short" with the exact miles missing,
never just "not enough". Scoot-only cities keep "(Scoot)" and get the Economy line only, since
Scoot has no business class. Leave out any city with no KrisFlyer award.)

🎯 Closest next: Tokyo business return, 9,000 short

💡 Before you book
• Check a seat on singaporeair.com first, then transfer (transfers can't be undone)
• (at most 2 more short tips, e.g. taxes, transfer time)

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
        "QUESTION",
        question,
    ])


def run_claude(prompt, tools=("WebSearch", "WebFetch")):
    cmd = [os.environ.get("CLAUDE_BIN") or "claude", "-p"]
    if tools:
        cmd += ["--allowedTools", *tools]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
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


def telegram_api(token, method, payload, timeout=30):
    """Call a Telegram Bot API method and return its result, or raise RuntimeError."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram {method} failed: {error.read().decode(errors='replace')}")
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
    telegram_api(token, "sendChatAction", payload, timeout=5)  # short: runs in the heartbeat loop


def send_telegram(text, token, chat_id, silent=False):
    chat, topic = split_chat_id(chat_id)
    for i, piece in enumerate(chunks(text)):
        payload = {
            "chat_id": chat,
            "text": piece,
            "disable_web_page_preview": True,
            "disable_notification": silent or i > 0,
        }
        if topic:
            payload["message_thread_id"] = topic
        telegram_api(token, "sendMessage", payload)


def send_report(report, token, chat_id, split):
    """Post a report, one message per section when split; only the first one notifies."""
    messages = sections(report) if split else [report]
    for i, message in enumerate(messages):
        send_telegram(message, token, chat_id, silent=i > 0)
    return len(messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=sorted([*PROMPTS, "reminders", "news", "ask", "tickets"]))
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

    if args.mode == "news":
        import news_watch
        send = (lambda text: send_telegram(text, token, chat_id)) if posting else print
        try:
            count, errors = news_watch.check(send, save=bool(posting))
        except (RuntimeError, OSError) as error:
            fail(str(error))
        for error in errors:
            print(f"warning: couldn't read {error}", file=sys.stderr)
        print(f"Found {count} new post(s).")
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
        if not posting:
            print(answer)
            return
        try:
            # The ticket list is laid out in blocks, so it posts one message per section.
            send_report(answer, token, chat_id, args.mode == "tickets" and split == "sections")
        except (RuntimeError, OSError) as error:
            fail(str(error))
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

    if args.mode == "daily" and status == "QUIET" and quiet != "off" and not args.full:
        if quiet == "silent":
            print("Quiet day: nothing posted.")
            return
        report = (f"✈️ {today:%d %b}: no fare changes or deals on your watchlist today. "
                  "Send /run daily for the full report.")
    else:
        report = shorten(report, args.mode)

    if not posting:
        if split == "sections":
            print("\n\n────────── next message ──────────\n\n".join(sections(report)))
        else:
            print(report)
        return
    try:
        count = send_report(report, token, chat_id, split == "sections")
    except (RuntimeError, OSError) as error:
        fail(str(error))
    print(f"Posted the {args.mode} report to Telegram ({len(report)} characters, "
          f"{count} message{'s' if count != 1 else ''}).")


if __name__ == "__main__":
    main()
