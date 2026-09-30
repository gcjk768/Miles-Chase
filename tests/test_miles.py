"""Tests for the miles runner, Telegram commands, reminders and scheduler.

Run with: python3 -m unittest discover -s tests -v
No network, Claude or Telegram needed: file paths point at a temporary folder.
"""

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


class Schedule(unittest.TestCase):
    @staticmethod
    def at(*args):
        return datetime(*args, tzinfo=run_miles.SGT)

    def test_due_jobs(self):
        self.assertEqual(scheduler.due_jobs(self.at(2026, 10, 1, 7, 0), {}), [])
        self.assertEqual(scheduler.due_jobs(self.at(2026, 10, 1, 7, 53), {}), [("daily", "2026-10-01")])
        self.assertEqual(
            [mode for mode, _ in scheduler.due_jobs(self.at(2026, 10, 1, 9, 0), {})],
            ["daily", "monthly", "reminders"])
        done = {"daily": "2026-10-01", "monthly": "2026-10", "reminders": "2026-10-01"}
        self.assertEqual(scheduler.due_jobs(self.at(2026, 10, 1, 12, 0), done), [])
        self.assertEqual(
            [mode for mode, _ in scheduler.due_jobs(self.at(2026, 10, 2, 9, 0), done)],
            ["daily", "reminders"])


if __name__ == "__main__":
    unittest.main()
