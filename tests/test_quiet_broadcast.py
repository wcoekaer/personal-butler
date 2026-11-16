import datetime as dt
import tempfile
import unittest
from pathlib import Path

import jarvis


class OnlyIfIssues(unittest.TestCase):
    """The 22:00 check speaks only when something needs attention."""

    def setUp(self):
        self.calls = []
        self.orig = (jarvis.generate_text, jarvis.synthesize, jarvis.JOURNAL)
        jarvis.JOURNAL = None
        jarvis.synthesize = lambda text, cfg, workdir: self.calls.append(text) or Path("/tmp/x.wav")
        self.cfg = {"location": "Home", "failure_message": "Apologies."}
        self.b = {"name": "security", "homepods": ["Kitchen"], "prompt": "check", "only_if_issues": True,
                  "all_clear": "All clear, sir."}

    def tearDown(self):
        jarvis.generate_text, jarvis.synthesize, jarvis.JOURNAL = self.orig

    def run_with(self, reply, scheduled):
        jarvis.generate_text = (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))) if reply is None else (lambda *a, **k: reply)
        with tempfile.TemporaryDirectory() as tmp:
            return jarvis.prepare_broadcast(self.b, self.cfg, dt.datetime.now(), Path(tmp), False, scheduled)

    def test_scheduled_all_clear_and_failures_are_silent(self):
        self.assertIsNone(self.run_with("ALL CLEAR", True)[1])
        self.assertIsNone(self.run_with("All clear.", True)[1])
        self.assertIsNone(self.run_with(None, True)[1])          # LLM down: no apology at 10 PM
        self.assertEqual(self.calls, [])

    def test_issues_are_spoken_and_manual_runs_always_answer(self):
        self.assertIsNotNone(self.run_with("The garage door is open, sir. Good night.", True)[1])
        self.assertEqual(self.run_with("ALL CLEAR", False)[0], "All clear, sir.")
        self.assertEqual(self.calls, ["The garage door is open, sir. Good night.", "All clear, sir."])

    def test_other_broadcasts_unchanged(self):
        self.b.pop("only_if_issues")
        self.assertEqual(self.run_with("ALL CLEAR", True)[0], "ALL CLEAR")


if __name__ == "__main__":
    unittest.main()
