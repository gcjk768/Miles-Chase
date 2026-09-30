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
    /help                          list the commands
"""

import re

import run_miles

OFFSET = run_miles.STATE / "telegram_offset.txt"
HELP = (
    "Commands:\n"
    "/points: show balances\n"
    "/points CR 52000: set a balance (CR, CPM, SCR or KF)\n"
    "/points SCR 31000 exp 2027-06: set a balance and expiry\n"
    "/watch: list routes\n"
    "/watch add SIN Bali, Jun 2027: add a route\n"
    "/watch remove 2: remove route 2\n"
    "/goal: show the goal\n"
    "/goal Tokyo business, 2 pax, Mar 2027: set the goal\n"
    "/goal clear: remove the goal\n"
    "/run daily or /run monthly: run a report now"
)


def poll(token, chat_id, run_report, timeout=20):
    """Wait up to `timeout` seconds for new messages and handle any commands."""
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    updates = run_miles.telegram_api(token, "getUpdates", {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["message", "channel_post"],
    }, timeout=timeout + 10)
    for update in updates:
        # Save the offset first, so a command that crashes isn't handled again on restart.
        OFFSET.parent.mkdir(parents=True, exist_ok=True)
        OFFSET.write_text(str(update["update_id"] + 1))
        message = update.get("message") or update.get("channel_post")
        if not message or str(message["chat"]["id"]) != str(chat_id):
            continue
        reply = handle(message.get("text", ""), run_report)
        if reply:
            run_miles.send_telegram(reply, token, chat_id)


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
        return "Use /run daily or /run monthly"
    if command in ("/help", "/start"):
        return HELP
    return None


def points_command(args):
    if not args:
        return balances_summary()
    usage = "Use /points CR 52000, optionally followed by exp YYYY-MM"
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
        return str(error)
    return f"Updated {args[0].upper()}.\n\n" + balances_summary()


def watch_command(args):
    usage = "Use /watch, /watch add SIN Bali, Jun 2027 or /watch remove 2"
    try:
        if not args:
            pass
        elif args[0].lower() == "add":
            route = " ".join(args[1:])
            run_miles.add_route(route)
            return f"Added {route}.\n\n" + watchlist_summary()
        elif args[0].lower() == "remove" and len(args) == 2 and args[1].isdigit():
            removed = run_miles.remove_route(int(args[1]))
            return f"Removed {removed}.\n\n" + watchlist_summary()
        else:
            return usage
    except ValueError as error:
        return str(error)
    return watchlist_summary()


def watchlist_summary():
    routes = run_miles.watchlist_routes()
    if not routes:
        return "The watchlist is empty. Add a route with /watch add SIN Bali, Jun 2027"
    return "Watchlist:\n" + "\n".join(f"{i}. {route}" for i, route in enumerate(routes, 1))


def goal_command(args):
    try:
        if not args:
            goal = run_miles.get_goal()
            return f"Goal: {goal}" if goal else "No goal set. Use /goal Tokyo business, 2 pax, Mar 2027"
        if len(args) == 1 and args[0].lower() == "clear":
            run_miles.set_goal(None)
            return "Goal removed."
        goal = " ".join(args)
        run_miles.set_goal(goal)
        return f"Goal set: {goal}. The next monthly report will track it."
    except ValueError as error:
        return str(error)


def balances_summary():
    lines = run_miles.read_lines(run_miles.MY_POINTS)
    summary = run_miles.miles_summary(run_miles.parse_balances(lines))
    return "\n".join(lines + ["", "If you convert everything:"] + summary)
