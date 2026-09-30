"""Tests for the miles runner, Telegram commands, reminders and scheduler.

Run with: python3 -m unittest discover -s tests -v
No network, Claude or Telegram needed: file paths point at a temporary folder.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reminders  # noqa: E402
import run_miles  # noqa: E402
import scheduler  # noqa: E402
import news_watch  # noqa: E402
import telegram_bot  # noqa: E402

POINTS = """# comment
CR: 52000 exp 2027-01
CPM: 20000
SCR: 31000 exp 2027-06
KF: 12000 exp 2029-01
# Goal: example
"""
WATCHLIST = """# Routes
SIN Tokyo, Mar 2027
SIN London, flexible
"""


class TempFiles(unittest.TestCase):
    """Point every data and state file at a fresh temporary folder."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.saved = {}
        paths = {
            (run_miles, "MY_POINTS"): self.dir / "my_points.txt",
            (run_miles, "WATCHLIST"): self.dir / "watchlist.txt",
            (run_miles, "PRICES_HISTORY"): self.dir / "prices.txt",
            (run_miles, "MONTHLY_HISTORY"): self.dir / "monthly_history.txt",
            (reminders, "SENT"): self.dir / "reminders_sent.json",
            (telegram_bot, "OFFSET"): self.dir / "offset.txt",
            (news_watch, "SEEN"): self.dir / "news_seen.json",
            (scheduler, "LAST_RUN"): self.dir / "scheduler.json",
            (scheduler, "HEARTBEAT"): self.dir / "heartbeat",
        }
        for (module, name), path in paths.items():
            self.saved[(module, name)] = getattr(module, name)
            setattr(module, name, path)
        run_miles.MY_POINTS.write_text(POINTS)
        run_miles.WATCHLIST.write_text(WATCHLIST)

    def tearDown(self):
        for (module, name), value in self.saved.items():
            setattr(module, name, value)
        shutil.rmtree(self.dir)


class MilesAndFees(unittest.TestCase):
    BALANCES = {"CR": 52000, "CPM": 20000, "SCR": 31000, "KF": 12000}

    def test_points_convert_before_transfer(self):
        self.assertEqual(run_miles.card_miles("CR", 52000), 20800)
        self.assertEqual(run_miles.card_miles("SCR", 31000), 12400)
        self.assertEqual(run_miles.card_miles("CPM", 20000), 20000)
        self.assertEqual(run_miles.miles_available(self.BALANCES), 65200)

    def test_fees_are_one_per_card_with_points(self):
        self.assertEqual(run_miles.transfer_fee("CR", 52000), 27.25)
        self.assertEqual(run_miles.transfer_fee("CR", 0), 0)
        self.assertEqual(run_miles.transfer_fee("KF", 12000), 0)
        summary = run_miles.miles_summary(self.BALANCES)
        self.assertIn("Total: 65,200 miles", summary)
        self.assertIn("Transfer fees to pay: about S$81.50 (one transfer per card)", summary)
        self.assertIn("KF: 12,000 miles already in KrisFlyer", summary)
        text = "\n".join(summary)
        self.assertIn("CPM: 20,000 Citi Miles = 20,000 KrisFlyer miles", text)
        self.assertIn("CR: 52,000 points = 20,800 KrisFlyer miles", text)

    def test_parse_balances_ignores_other_lines(self):
        lines = ["CR: 52,000 exp 2027-01", "Goal: Tokyo", "Family: Dad KF 30000", "XX: 5"]
        self.assertEqual(run_miles.parse_balances(lines), {"CR": 52000})


class ReportCleaning(unittest.TestCase):
    def test_hides_account_numbers_but_not_dates_prices_or_links(self):
        text = ("**KF 8812345678** card 4111 1111 1111 1111\n## Deals\n"
                "2026-09-30 2026-10-01 S$1,234 108,500\nhttps://x.com/p/12345678901234")
        cleaned = run_miles.clean_report(text, "daily")
        self.assertNotIn("8812345678", cleaned)
        self.assertNotIn("4111", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertIn("\nDeals\n", cleaned)
        self.assertIn("2026-09-30 2026-10-01 S$1,234 108,500", cleaned)
        self.assertIn("https://x.com/p/12345678901234", cleaned)

    def test_prices_line_with_or_without_brackets(self):
        for line in ("PRICES 2026-09-30 | SIN Tokyo Y 850 J NA",
                     "PRICES [2026-09-30] | SIN Tokyo Y 850 J NA"):
            report, prices = run_miles.split_prices_line("hello\n" + line)
            self.assertEqual(report, "hello")
            self.assertEqual(prices, line)
        self.assertIsNone(run_miles.split_prices_line("no prices")[1])

    def test_status_line(self):
        self.assertEqual(run_miles.split_status("STATUS: QUIET\nbody"), ("body", "QUIET"))
        self.assertEqual(run_miles.split_status("status: news\nbody"), ("body", "NEWS"))
        self.assertEqual(run_miles.split_status("body"), ("body", None))

    def test_sections_split_on_blank_lines_and_keep_title_with_next(self):
        long_text = "x" * 250
        report = (f"✈️ TITLE\n\n🚨 Alerts\n{long_text}\n\n\n🗼 Tokyo\n{long_text}\n\n"
                  f"🦘 Sydney\n{long_text}")
        self.assertEqual(run_miles.sections(report), [
            f"✈️ TITLE\n\n🚨 Alerts\n{long_text}", f"🗼 Tokyo\n{long_text}", f"🦘 Sydney\n{long_text}"])
        self.assertEqual(run_miles.sections("one line"), ["one line"])

    def test_short_sections_share_a_message(self):
        long_text = "x" * 250
        report = (f"💰 BALANCES\n{long_text}\n\n🤝 BACKUP\nshort\n\n📅 TIMING\nshort\n\n"
                  f"💳 CARDS\n{long_text}\n\n🎁 DEALS\nnone\n\n💡 TIP\ntip\n\n🔗 SOURCES\na.com")
        self.assertEqual(run_miles.sections(report), [
            f"💰 BALANCES\n{long_text}",
            "🤝 BACKUP\nshort\n\n📅 TIMING\nshort",
            f"💳 CARDS\n{long_text}",
            "🎁 DEALS\nnone\n\n💡 TIP\ntip\n\n🔗 SOURCES\na.com",
        ])

    def test_grouping_stops_at_the_limit(self):
        block = "🎁 S\n" + "y" * 190
        messages = run_miles.sections("\n\n".join([block] * 6))
        self.assertTrue(all(len(m) <= run_miles.GROUP_LIMIT for m in messages))
        self.assertEqual(sum(m.count("🎁") for m in messages), 6)

    def test_send_report_only_first_message_notifies(self):
        sent = []
        original = run_miles.telegram_api
        run_miles.telegram_api = lambda token, method, payload, timeout=30: sent.append(payload)
        try:
            long_text = "x" * 250
            report = f"A\n{long_text}\n\nB\n{long_text}\n\nC\n{long_text}"
            count = run_miles.send_report(report, "t", "c", split=True)
            self.assertEqual(count, 3)
            self.assertEqual([p["disable_notification"] for p in sent], [False, True, True])
            sent.clear()
            self.assertEqual(run_miles.send_report("A\nx\n\nB\ny", "t", "c", split=False), 1)
            self.assertEqual(len(sent), 1)
        finally:
            run_miles.telegram_api = original

    def test_chunks_fit_telegram(self):
        text = "\n".join("line %d " % i + "x" * 300 for i in range(40)) + "\n" + "y" * 9000
        pieces = run_miles.chunks(text)
        self.assertTrue(all(len(p) <= run_miles.TELEGRAM_LIMIT for p in pieces))
        self.assertEqual("".join(pieces).replace("\n", ""), text.replace("\n", ""))


class DataFiles(TempFiles):
    def test_set_balance_keeps_expiry_and_comments(self):
        run_miles.set_balance("cr", 60000)
        text = run_miles.MY_POINTS.read_text()
        self.assertIn("CR: 60000 exp 2027-01", text)
        self.assertIn("# comment", text)
        run_miles.set_balance("SCR", 1000, "2028-02")
        self.assertIn("SCR: 1000 exp 2028-02", run_miles.MY_POINTS.read_text())
        with self.assertRaises(ValueError):
            run_miles.set_balance("XX", 5)

    def test_set_balance_drops_template_placeholder_expiry(self):
        run_miles.MY_POINTS.write_text("CR: [points] exp [YYYY-MM]\n")
        run_miles.set_balance("CR", 1000)
        self.assertEqual(run_miles.MY_POINTS.read_text(), "CR: 1000\n")

    def test_prices_saved_once_per_day(self):
        today = date(2026, 9, 30)
        run_miles.save_prices("PRICES [2026-09-30] | a", today)
        run_miles.save_prices("PRICES 2026-09-30 | b", today)
        self.assertEqual(run_miles.PRICES_HISTORY.read_text(), "PRICES 2026-09-30 | b\n")

    def test_daily_prompt_uses_calculated_miles_and_yesterday(self):
        run_miles.PRICES_HISTORY.write_text(
            "PRICES 2026-09-28 | old\nPRICES 2026-09-29 | yesterday\nPRICES 2026-09-30 | today\n")
        prompt = run_miles.build_daily(date(2026, 9, 30))
        self.assertIn("MY MILES: 65200", prompt)
        self.assertIn("PRICES 2026-09-29 | yesterday", prompt)
        self.assertNotIn("| today", prompt)

    def test_monthly_prompt_includes_miles_and_fees(self):
        prompt, _ = run_miles.build_monthly(date(2026, 9, 30))
        self.assertIn("Total: 65,200 miles", prompt)
        self.assertIn("Transfer fees to pay: about S$81.50", prompt)


class TelegramCommands(TempFiles):
    def setUp(self):
        super().setUp()
        self.runs = []

    def handle(self, text):
        return telegram_bot.handle(text, lambda *args: self.runs.append(args))

    def test_points(self):
        self.assertIn("Total: 65,200 miles", self.handle("/points"))
        self.assertIn("Total: 68,400 miles", self.handle("/points CR 60,000"))
        reply = self.handle("/points SCR 31000 exp 2027-07")
        self.assertIn("SCR end Jul 2027", reply)
        self.assertIn("SCR: 31000 exp 2027-07", run_miles.MY_POINTS.read_text())
        self.assertEqual(reply.count("31,000"), 1)  # balances aren't listed twice
        self.assertTrue(self.handle("/points CR abc").startswith("Use /points"))
        self.assertIn("unknown card", self.handle("/points XX 5"))

    def test_watch(self):
        self.assertIn("1. SIN Tokyo, Mar 2027", self.handle("/watch"))
        self.assertIn("3. SIN Bali, Jun 2027", self.handle("/watch add SIN Bali, Jun 2027"))
        self.assertIn("already on the watchlist", self.handle("/watch add sin bali, jun 2027"))
        self.assertIn("Removed SIN London, flexible", self.handle("/watch remove 2"))
        self.assertEqual(run_miles.watchlist_routes(), ["SIN Tokyo, Mar 2027", "SIN Bali, Jun 2027"])
        self.assertIn("# Routes", run_miles.WATCHLIST.read_text())
        self.assertIn("Pick a number", self.handle("/watch remove 9"))
        self.assertIn("can't contain", self.handle("/watch add SIN [x]"))
        self.handle("/watch remove 1")
        self.assertIn("only route", self.handle("/watch remove 1"))

    def test_goal(self):
        self.assertIn("No goal set", self.handle("/goal"))
        self.handle("/goal Tokyo business, 2 pax, Mar 2027")
        self.assertEqual(run_miles.get_goal(), "Tokyo business, 2 pax, Mar 2027")
        self.handle("/goal Seoul economy")
        self.assertEqual(run_miles.MY_POINTS.read_text().count("Goal:"), 2)  # new one + comment
        self.assertEqual(run_miles.get_goal(), "Seoul economy")
        self.assertEqual(self.handle("/goal clear"), "Goal removed.")
        self.assertIsNone(run_miles.get_goal())

    def test_run_and_other_messages(self):
        self.assertIsNone(self.handle("/run daily"))
        self.assertIsNone(self.handle("/run@MilesBot monthly"))
        self.assertEqual(self.runs, [("daily", "--full"), ("monthly",)])
        self.assertTrue(self.handle("/run weekly").startswith("Use /run"))
        self.assertIsNone(self.handle("hello"))
        self.assertIn("/watch", self.handle("/help"))

    def test_poll_only_reads_own_chat(self):
        sent = []
        original = run_miles.telegram_api

        def fake_api(token, method, payload, timeout=30):
            if method == "getUpdates":
                return [
                    {"update_id": 5, "channel_post": {"chat": {"id": -100}, "text": "/points KF 13000"}},
                    {"update_id": 6, "message": {"chat": {"id": 999}, "text": "/points KF 1"}},
                ]
            sent.append(payload["text"])
            return {}

        run_miles.telegram_api = fake_api
        try:
            telegram_bot.poll("token", "-100", lambda *a: None)
        finally:
            run_miles.telegram_api = original
        self.assertEqual(len(sent), 1)
        self.assertIn("KF: 13000 exp 2029-01", run_miles.MY_POINTS.read_text())
        self.assertEqual(telegram_bot.OFFSET.read_text(), "7")

    def test_ask(self):
        self.assertIsNone(self.handle("/ask Transfer CR now for Tokyo?"))
        self.assertEqual(self.runs, [("ask", "Transfer CR now for Tokyo?")])
        self.assertIn("Give a question", self.handle("/ask"))
        self.assertIn("under 500", self.handle("/ask " + "x" * 501))
        self.assertEqual(len(self.runs), 1)  # bad questions never become jobs
        prompt = run_miles.build_ask(date(2026, 9, 30), "Transfer CR now?")
        self.assertIn("Total: 65,200 miles", prompt)
        self.assertTrue(prompt.rstrip().endswith("Transfer CR now?"))

    def test_points_update_lists_tickets_once(self):
        events = []
        original = run_miles.telegram_api

        def fake_api(token, method, payload, timeout=30):
            if method == "getUpdates":
                return [{"update_id": i, "message": {"chat": {"id": -100}, "text": text}}
                        for i, text in enumerate(["/points CR 60000", "/points KF 1000", "/points"])]
            events.append("reply")
            return {}

        run_miles.telegram_api = fake_api
        try:
            telegram_bot.poll("token", "-100", lambda *a: events.append(a))
        finally:
            run_miles.telegram_api = original
        self.assertEqual(events, ["reply"] * 3 + [("tickets",)])
        self.assertIsNone(self.handle("/tickets"))
        prompt = run_miles.build_ask(date(2026, 9, 30), "", "tickets")
        self.assertIn("BUSINESS SAVER", prompt)
        self.assertIn(run_miles.BOOK_URL, prompt)
        for city in ("Xiamen", "Seoul", "Osaka", "Sapporo"):
            self.assertIn(city, prompt)

    def test_topic_chat_id(self):
        self.assertEqual(run_miles.split_chat_id("-100123/2765"), ("-100123", 2765))
        self.assertEqual(run_miles.split_chat_id(-100123), ("-100123", None))
        sent = []
        original = run_miles.telegram_api

        def fake_api(token, method, payload, timeout=30):
            if method == "getUpdates":
                return [
                    {"update_id": 1, "message": {"chat": {"id": -100}, "message_thread_id": 7,
                                                 "text": "/goal"}},
                    {"update_id": 2, "message": {"chat": {"id": -100}, "message_thread_id": 9,
                                                 "text": "/goal"}},
                    {"update_id": 3, "message": {"chat": {"id": -100}, "text": "/goal"}},
                ]
            sent.append(payload)
            return {}

        run_miles.telegram_api = fake_api
        try:
            telegram_bot.poll("token", "-100/7", lambda *a: None)
        finally:
            run_miles.telegram_api = original
        self.assertEqual(len(sent), 1)  # only the command in topic 7 is answered
        self.assertEqual((sent[0]["chat_id"], sent[0]["message_thread_id"]), ("-100", 7))


class ExpiryReminders(TempFiles):
    LINES = ["CR: 52000 exp 2027-01", "KF: 12000 exp 2027-01", "CPM: 20000", "SCR: 0 exp 2027-01"]

    def test_expiry_counts_to_end_of_month(self):
        self.assertIn(("CR", 52000, date(2027, 1, 31)), reminders.expiries(self.LINES))

    def test_thresholds(self):
        self.assertEqual(reminders.due(date(2026, 11, 1), self.LINES, set()), [])  # 91 days
        due = reminders.due(date(2026, 12, 1), self.LINES, set())  # 61 days
        self.assertEqual(due, [])
        due = reminders.due(date(2026, 12, 2), self.LINES, set())  # 60 days
        self.assertEqual([keys[0] for keys, _ in due], ["CR:2027-01-31:60", "KF:2027-01-31:60"])
        self.assertIn("52,000 points (20,800 KrisFlyer miles)", due[0][1])
        self.assertIn("Transfer fee S$27.25", due[0][1])
        self.assertIn("12,000 miles expire", due[1][1])

    def test_each_reminder_sent_once_and_late_start_sends_one(self):
        run_miles.MY_POINTS.write_text("\n".join(self.LINES) + "\n")
        today = date(2027, 1, 11)  # 20 days before expiry
        messages = []
        self.assertEqual(reminders.run(today, messages.append), 2)
        self.assertEqual(reminders.run(today, messages.append), 0)
        sent = reminders.load_sent()
        self.assertIn("CR:2027-01-31:30", sent)
        self.assertIn("CR:2027-01-31:60", sent)  # skipped, not sent later
        self.assertEqual(reminders.run(date(2027, 1, 24), messages.append), 2)  # 7 days
        self.assertEqual(reminders.run(date(2027, 2, 1), messages.append), 0)  # expired
        self.assertEqual(len(messages), 4)


class Schedule(TempFiles):
    @staticmethod
    def at(*args):
        return datetime(*args, tzinfo=run_miles.SGT)

    def modes(self, now, last_run):
        return [mode for mode, _ in scheduler.due_jobs(now, last_run) if mode != "news"]

    def test_due_jobs(self):
        self.assertEqual(self.modes(self.at(2026, 10, 1, 7, 0), {}), [])
        self.assertEqual(self.modes(self.at(2026, 10, 1, 8, 7), {}), ["monthly"])
        self.assertEqual(self.modes(self.at(2026, 10, 1, 9, 0), {}), ["monthly", "reminders"])
        self.assertEqual(self.modes(self.at(2026, 10, 1, 18, 0), {}), ["daily", "monthly", "reminders"])
        done = {"daily": "2026-10-01", "monthly": "2026-10", "reminders": "2026-10-01"}
        self.assertEqual(self.modes(self.at(2026, 10, 1, 20, 0), done), [])
        self.assertEqual(self.modes(self.at(2026, 10, 2, 9, 0), done), ["reminders"])
        self.assertEqual(self.modes(self.at(2026, 10, 2, 18, 0), done), ["daily", "reminders"])

    def test_news_runs_once_per_slot(self):
        first = dict(scheduler.due_jobs(self.at(2026, 10, 1, 10, 5), {}))["news"]
        same_slot = scheduler.due_jobs(self.at(2026, 10, 1, 10, 29), {"news": first})
        self.assertNotIn("news", dict(same_slot))
        self.assertIn("news", dict(scheduler.due_jobs(self.at(2026, 10, 1, 10, 30), {"news": first})))

    def test_corrupted_schedule_file_starts_fresh(self):
        scheduler.LAST_RUN.write_text("{not json")
        last_run = scheduler.load_last_run(self.at(2026, 10, 1, 19, 0))
        self.assertEqual(last_run.get("daily"), "2026-10-01")  # today's jobs aren't re-fired
        self.assertNotIn("news", last_run)


class JobFailures(TempFiles):
    """How the scheduler reacts when a job fails, with fake jobs, repair and Telegram."""

    def setUp(self):
        super().setUp()
        self.sent, self.repairs, self.outputs = [], [], []
        self.saved_fns = (scheduler.run_job, scheduler.telegram)
        scheduler.run_job = lambda mode, *options: self.outputs.pop(0)
        scheduler.telegram = self.sent.append
        scheduler.retries.clear()
        scheduler.repaired_today.clear()
        scheduler.last_reported.clear()
        import repair
        self.repair_module, self.saved_repair = repair, repair.repair

        def fake_repair(mode, output):
            self.repairs.append(mode)
            return "Cause: bad line\nFixed: data/my_points.txt\nYou need to: nothing"
        repair.repair = fake_repair

    def tearDown(self):
        scheduler.run_job, scheduler.telegram = self.saved_fns
        self.repair_module.repair = self.saved_repair
        super().tearDown()

    def test_success(self):
        self.outputs = [(0, "Posted")]
        scheduler.run("daily")
        self.assertEqual((self.sent, self.repairs, scheduler.retries), ([], [], {}))

    def test_temporary_problem_is_retried_then_reported(self):
        self.outputs = [(1, "urlopen error timed out")]
        scheduler.run("daily")
        self.assertIn("daily", scheduler.retries)
        self.assertEqual(self.sent, [])
        self.outputs = [(1, "HTTP 503")]
        scheduler.run("daily", attempt=scheduler.MAX_RETRIES + 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("temporary problem", self.sent[0])
        self.assertEqual(self.repairs, [])

    def test_sign_in_problem_asks_for_a_new_token(self):
        self.outputs = [(1, "API Error: 401 authentication_error Invalid bearer token")]
        scheduler.run("monthly")
        self.assertIn("claude setup-token", self.sent[0])
        self.assertEqual(self.repairs, [])

    def test_other_failure_gets_one_repair_and_rerun(self):
        self.outputs = [(1, "error: data/my_points.txt still has a placeholder"), (0, "Posted")]
        scheduler.run("daily")
        self.assertEqual(self.repairs, ["daily"])
        self.assertIn("Self-repair by Claude", self.sent[0])
        self.assertIn("Re-ran it and it worked", self.sent[0])
        # A second failure the same day isn't repaired again, just reported.
        self.outputs = [(1, "error: something else")]
        scheduler.last_reported.clear()
        scheduler.run("daily")
        self.assertEqual(self.repairs, ["daily"])
        self.assertEqual(self.sent[-1], "⚠️ Miles daily failed.\nerror: something else")

    def test_self_repair_can_be_switched_off(self):
        import os
        os.environ["SELF_REPAIR"] = "off"
        try:
            self.outputs = [(1, "error: broken")]
            scheduler.run("daily")
        finally:
            del os.environ["SELF_REPAIR"]
        self.assertEqual(self.repairs, [])
        self.assertEqual(len(self.sent), 1)

    def test_execute_times_out_and_keeps_heartbeat(self):
        code, output = scheduler.execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(code, -1)
        self.assertIn("timed out", output)
        self.assertTrue(scheduler.HEARTBEAT.exists())

    def test_typing_shown_while_claude_jobs_run(self):
        calls = []
        original = run_miles.telegram_api

        def fake_api(token, method, payload, timeout=30):
            calls.append((method, payload))
            raise RuntimeError("Telegram down")  # must not break the job

        run_miles.telegram_api = fake_api
        os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="-100/7")
        try:
            quick = [sys.executable, "-c", "import time; time.sleep(1)"]
            self.assertEqual(scheduler.execute(quick, typing=True)[0], 0)
            self.assertEqual(calls, [("sendChatAction",
                                      {"chat_id": "-100", "action": "typing", "message_thread_id": 7})])
            calls.clear()
            scheduler.execute(quick)
            self.assertEqual(calls, [])
        finally:
            run_miles.telegram_api = original
            del os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        self.assertIn("tickets", scheduler.CLAUDE_MODES)
        self.assertNotIn("news", scheduler.CLAUDE_MODES)


class NewsAlerts(TempFiles):
    FEED_A = [("KrisFlyer Spontaneous Escapes October", "https://a/1", "30% off Saver"),
              ("Best coffee in town", "https://a/2", "nothing to do with miles")]

    def setUp(self):
        super().setUp()
        self.all_feeds = {**news_watch.FEEDS, **news_watch.VIDEO_FEEDS, **news_watch.MIXED_VIDEO_FEEDS}
        self.feeds = {name: list(self.FEED_A) for name in self.all_feeds}
        self.sent = []

    def fetch(self, url):
        name = next(n for n, u in self.all_feeds.items() if u == url)
        if self.feeds[name] is None:
            raise OSError("feed down")
        return self.feeds[name]

    def check(self):
        return news_watch.check(self.sent.append, fetch=self.fetch)

    def test_first_read_only_records(self):
        self.assertEqual(self.check(), (0, []))
        self.assertEqual(self.sent, [])

    def test_new_matching_posts_are_sent_once(self):
        self.check()
        name = next(iter(news_watch.FEEDS))
        self.feeds[name] = [("Citi transfer bonus to KrisFlyer", "https://a/3", ""),
                            ("New cafe opens", "https://a/4", "")] + self.FEED_A
        self.assertEqual(self.check()[0], 1)
        self.assertIn("Citi transfer bonus to KrisFlyer", self.sent[0])
        self.assertIn("https://a/3", self.sent[0])
        self.assertNotIn("New cafe", self.sent[0])
        self.assertEqual(self.check()[0], 0)  # not sent again

    def test_videos(self):
        self.check()
        self.feeds["Suitesmile (YouTube)"] = [("Lounge tour", "https://y/1", "")] + self.FEED_A
        self.feeds["HoneyMoneySG"] = [("Best miles card 2026", "https://y/2", ""),
                                      ("Pick ETFs, not stocks", "https://y/3", "")] + self.FEED_A
        self.assertEqual(self.check()[0], 2)
        self.assertEqual(len(self.sent), 1)  # no blog posts, so only the video message
        self.assertTrue(self.sent[0].startswith("🎥 New miles videos"))
        self.assertIn("https://y/1", self.sent[0])  # miles channel: every video
        self.assertIn("https://y/2", self.sent[0])  # mixed channel: only miles videos
        self.assertNotIn("ETFs", self.sent[0])

    def test_youtube_atom_feed_parses(self):
        xml = (b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">'
               b'<entry><title>KrisFlyer tips</title><link rel="alternate" href="https://y/9"/>'
               b'<media:group><media:description>Saver awards</media:description></media:group>'
               b'</entry></feed>')

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        original = news_watch.urllib.request.urlopen
        news_watch.urllib.request.urlopen = lambda *a, **k: Response(xml)
        try:
            self.assertEqual(news_watch.fetch("https://y"), [("KrisFlyer tips", "https://y/9", "Saver awards")])
        finally:
            news_watch.urllib.request.urlopen = original

    def test_watchlist_cities_match_and_short_words_dont_misfire(self):
        self.check()
        name = next(iter(news_watch.FEEDS))
        self.feeds[name] = [("Tokyo hotel deals", "https://a/5", ""),
                            ("Asia's best rooftop bars", "https://a/6", "")]
        self.check()
        self.assertIn("Tokyo hotel deals", self.sent[0])
        self.assertNotIn("rooftop", self.sent[0])  # "SIA" doesn't match inside "Asia"

    def test_a_feed_down_on_first_read_doesnt_flood_later(self):
        names = list(news_watch.FEEDS)
        self.feeds[names[1]] = None
        count, errors = self.check()
        self.assertEqual((count, len(errors)), (0, 1))
        self.feeds[names[1]] = list(self.FEED_A)
        self.assertEqual(self.check()[0], 0)  # its old posts are only recorded

    def test_corrupted_seen_file(self):
        news_watch.SEEN.write_text("[broken")
        self.assertEqual(self.check(), (0, []))


if __name__ == "__main__":
    unittest.main()
