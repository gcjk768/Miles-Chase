"""Telegram commands for the miles bot, checked by scheduler.py.

Only messages in TELEGRAM_CHAT_ID (your private channel or chat with the bot) are read.

    /points                        show balances and the miles they add up to
    /points CR 52000               set a balance (CR, CPM, SCR or KF)
    /points SCR 31000 exp 2027-06  set a balance and its expiry
    /watch                         list the watchlist routes
    /watch add SIN Bali, Jun 2027  add a route
    /watch remove 2                remove route number 2
    /goal                          show the goal
    /goal Tokyo business, 2 pax, Mar 2027   set the goal
    /goal clear                    remove the goal
    /run daily   /run monthly      run a report now (daily always posts in full)
    /miles Transfer CR now?        ask Claude (claude -p) with your balances as context (/ask works too)
    /mileshelp                     list the commands (/help works too)
"""

import re

import reminders
import run_miles

OFFSET = run_miles.STATE / "telegram_offset.txt"
esc, card = run_miles.esc, run_miles.card
UPDATED = "✅ updated"  # subtitle of the /points reply after a balance change
HELP = card(
    "help", "commands",
    "💰 <b>Balances</b> · <code>/points</code>\n"
    "✏️ <code>/points CR 52000</code> set a balance (CR, CPM, SCR or KF); "
    "Claude then lists the tickets you can book\n"
    "⏳ <code>/points SCR 31000 exp 2027-06</code> balance and expiry",
    "🗺 <b>Watchlist</b> · <code>/watch</code>\n"
    "➕ <code>/watch add SIN Bali, Jun 2027</code>\n"
    "➖ <code>/watch remove 2</code>",
    "🎯 <b>Goal</b> · <code>/goal</code>\n"
    "✏️ <code>/goal Tokyo business, 2 pax, Mar 2027</code>\n"
    "🗑 <code>/goal clear</code>",
    "📊 <b>Reports</b> · <code>/run daily</code>  ·  <code>/run monthly</code>\n"
    "🎟 <code>/tickets</code> Business and Economy tickets your miles can book",
    "💬 <b>Ask Claude</b> · <code>/miles &lt;question&gt;</code>\n"
    "<i>Uses your balances; takes a minute or two.</i>",
)


def note(text):
    """A short usage hint or error as a card."""
    return card("note", "", f"💡 <i>{esc(text)}</i>")


def poll(token, chat_id, run_report, timeout=20):
    """Wait up to `timeout` seconds for new messages and handle any commands."""
    try:
        offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    except (ValueError, OSError):
        offset = 0
    updates = run_miles.telegram_api(token, "getUpdates", {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["message", "channel_post"],
    }, timeout=timeout + 10)
    balances_changed = False
    for update in updates:
        # Save the offset first, so a command that crashes isn't handled again on restart.
        OFFSET.parent.mkdir(parents=True, exist_ok=True)
        OFFSET.write_text(str(update["update_id"] + 1))
        message = update.get("message") or update.get("channel_post")
        chat, topic = run_miles.split_chat_id(chat_id)
        # In a group with topics, only commands sent in the bot's own topic count.
        if not message or str(message["chat"]["id"]) != chat or (
                topic and message.get("message_thread_id") != topic):
            continue
        reply = handle(message.get("text", ""), run_report)
        if reply:
            run_miles.send_telegram(reply, token, chat_id)
            balances_changed |= UPDATED in reply.split("\n", 1)[0]
    # After the balance reply, and once per batch, so four quick /points updates cost one Claude run.
    if balances_changed:
        run_report("tickets")


def handle(text, run_report):
    """Reply text for a command, or None if there's nothing to say."""
    words = text.split()
    if not words or not words[0].startswith("/"):
        return None
    command = words[0].split("@")[0].lower()  # /points@MyBot works too
    if command == "/points":
        return points_command(words[1:])
    if command == "/watch":
        return watch_command(words[1:])
    if command == "/goal":
        return goal_command(words[1:])
    if command == "/run":
        if len(words) == 2 and words[1].lower() in run_miles.PROMPTS:
            mode = words[1].lower()
            # Asked for by hand, so post the whole daily report even on a quiet day.
            run_report(mode, "--full") if mode == "daily" else run_report(mode)
            return None
        return note("Use /run daily or /run monthly")
    if command == "/tickets":
        run_report("tickets")
        return None
    if command in ("/miles", "/ask"):  # /miles is unique in the shared group menu
        question = " ".join(words[1:])
        try:
            run_miles.check_user_text(question, "question", limit=run_miles.ASK_LIMIT)
        except ValueError as error:
            return note(f"{error} Example: /miles Should I transfer CR now for Tokyo?")
        run_report("ask", question)
        return None
    if command in ("/mileshelp", "/help", "/start"):
        return HELP
    return None


def points_command(args):
    if not args:
        return balances_summary()
    usage = note("Use /points CR 52000, optionally followed by exp YYYY-MM")
    if len(args) not in (2, 4) or not re.fullmatch(r"[\d,]+", args[1]):
        return usage
    expiry = None
    if len(args) == 4:
        if args[2].lower() != "exp" or not re.fullmatch(r"\d{4}-\d{2}", args[3]):
            return usage
        expiry = args[3]
    try:
        run_miles.set_balance(args[0], int(args[1].replace(",", "")), expiry)
    except ValueError as error:
        return note(str(error))
    return balances_summary(f"{UPDATED} {args[0].upper()}")


def watch_command(args):
    usage = note("Use /watch, /watch add SIN Bali, Jun 2027 or /watch remove 2")
    try:
        if not args:
            pass
        elif args[0].lower() == "add":
            route = " ".join(args[1:])
            run_miles.add_route(route)
            return watchlist_summary(f"➕ added {route}")
        elif args[0].lower() == "remove" and len(args) == 2 and args[1].isdigit():
            removed = run_miles.remove_route(int(args[1]))
            return watchlist_summary(f"➖ removed {removed}")
        else:
            return usage
    except ValueError as error:
        return note(str(error))
    return watchlist_summary()


def watchlist_summary(subtitle=None):
    routes = run_miles.watchlist_routes()
    if not routes:
        return note("The watchlist is empty. Add a route with /watch add SIN Bali, Jun 2027")
    lines = "\n".join(f"✈️ <b>{i}</b> · {esc(route)}" for i, route in enumerate(routes, 1))
    return card("watch", subtitle or f"{len(routes)} route{'s' if len(routes) != 1 else ''}", lines)


def goal_command(args):
    try:
        if not args:
            goal = run_miles.get_goal()
            return (card("goal", "current", f"🎯 <b>{esc(goal)}</b>") if goal
                    else note("No goal set. Use /goal Tokyo business, 2 pax, Mar 2027"))
        if len(args) == 1 and args[0].lower() == "clear":
            run_miles.set_goal(None)
            return card("goal", "removed")
        goal = " ".join(args)
        run_miles.set_goal(goal)
        return card("goal", "✅ set", f"🎯 <b>{esc(goal)}</b>\n<i>The next monthly report will track it.</i>")
    except ValueError as error:
        return note(str(error))


def balances_summary(subtitle="if you convert everything"):
    """Miles and fees per card, expiry dates and the goal, each shown once."""
    lines = run_miles.read_lines(run_miles.MY_POINTS)
    *cards, total, fees = run_miles.miles_summary(run_miles.parse_balances(lines))
    blocks = ["\n".join(f"💳 {esc(line)}" for line in cards),
              f"🧮 <b>{esc(total)}</b>\n💵 {esc(fees)}"]
    expiring = [f"{card} end {expires:%b %Y}" for card, _, expires in reminders.expiries(lines)]
    if expiring:
        blocks.append("⏳ Expiry: " + esc(", ".join(expiring)))
    goal = run_miles.get_goal()
    if goal:
        blocks.append(f"🎯 Goal: {esc(goal)}")
    return card("points", subtitle, *blocks)
