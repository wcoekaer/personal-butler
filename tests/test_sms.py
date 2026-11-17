import asyncio
import base64
import os
import unittest
from pathlib import Path
from unittest import mock

import jarvis
import sms

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    def __init__(self, problems=None):
        self.sent, self.problems = [], problems or []

    async def send(self, to, body):
        self.sent.append((list(to), body))
        return self.problems


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def relay(clock=None, **cfg):
    base = {"enabled": True, "to": ["+15555550123"]}
    base.update(cfg)
    return sms.SmsRelay(base, "Jarvis", client=FakeClient(), clock=clock or Clock())


class Policy(unittest.TestCase):
    def test_alerts_and_conditions_texted_briefing_kinds_never_reach_it(self):
        r = relay()
        self.assertEqual(r.decide("alert", "drone", "A drone is nearby."), "Jarvis: A drone is nearby.")
        self.assertIsNotNone(r.decide("condition", "garage-left-open", "The garage is open."))
        self.assertIsNotNone(r.decide("condition_resolved", "garage-left-open", "The garage is closed."))
        self.assertIsNone(r.decide("say", "say", "Dinner is ready."))           # say: false by default
        self.assertIsNotNone(relay(say=True).decide("say", "say", "Dinner is ready."))
        self.assertIsNone(relay(conditions=False).decide("condition", "co2", "CO2 is high."))

    def test_exclude_only_and_quiet(self):
        self.assertIsNone(relay(exclude=["detections"]).decide("alert", "detections", "Person at the door."))
        r = relay(only=["drone"])
        self.assertIsNone(r.decide("alert", "detections", "Person at the door."))
        self.assertIsNotNone(r.decide("alert", "drone", "A drone."))
        self.assertIsNone(relay().decide("alert", "drone", "A drone.", quiet=True))
        self.assertIsNotNone(relay(during_quiet=True).decide("alert", "drone", "A drone.", quiet=True))

    def test_disabled_without_numbers_or_credentials(self):
        self.assertIsNone(relay(to=[]).decide("alert", "drone", "A drone."))
        with mock.patch.dict(os.environ, {"CLICKSEND_USERNAME": "", "CLICKSEND_API_KEY": ""}):
            r = sms.SmsRelay({"enabled": True, "to": ["+15555550123"]}, "Jarvis")
        self.assertFalse(r.enabled)

    def test_dedupe_cooldown_and_hourly_cap(self):
        clock = Clock()
        r = relay(clock, max_per_hour=3)
        self.assertIsNotNone(r.decide("alert", "a", "Same text."))
        self.assertIsNone(r.decide("alert", "a", "Same text."))                # repeat within 10 min
        clock.t += 601
        self.assertIsNotNone(r.decide("alert", "a", "Same text."))
        self.assertIsNotNone(r.decide("alert", "b", "Other text.", key="b:cam", cooldown=300))
        self.assertIsNone(r.decide("alert", "c", "Third text."))               # cap: 3 per hour
        clock.t += 3601
        self.assertIsNone(r.decide("alert", "b", "Other text 2.", key="b:cam", cooldown=7200))  # key cooldown
        self.assertIsNotNone(r.decide("alert", "c", "Third text."))


class Formatting(unittest.TestCase):
    def test_gsm_safe_and_shortened(self):
        r = relay(max_chars=40)
        body = r.decide("alert", "x", "It’s 54° — a “quiet” night with more words to cut here")
        self.assertTrue(body.startswith("Jarvis: It's 54 deg - a \"quiet\""))
        self.assertLessEqual(len(body), 40)
        self.assertTrue(body.endswith("..."))
        self.assertTrue(all(ord(c) < 128 for c in body))

    def test_prefix_template_and_mask(self):
        self.assertEqual(relay(prefix="[{assistant}] ").decide("alert", "x", "Hi"), "[Jarvis] Hi")
        self.assertEqual(sms.mask("+15555550123"), "...0123")


class ClickSendApi(unittest.TestCase):
    def test_request(self):
        c = sms.ClickSend("user@example.com", "KEY", sender="+15555550100")
        p = c.payload(["+15555550123", "+15555550124"], "Hello")
        self.assertEqual(p["messages"][0], {"source": "jarvis", "body": "Hello", "to": "+15555550123",
                                            "from": "+15555550100"})
        self.assertEqual(len(p["messages"]), 2)
        auth = c.headers()["Authorization"]
        self.assertEqual(base64.b64decode(auth.split()[1]).decode(), "user@example.com:KEY")

    def test_response_checks(self):
        ok = {"http_code": 200, "response_code": "SUCCESS",
              "data": {"messages": [{"to": "+15555550123", "status": "SUCCESS"}]}}
        self.assertEqual(sms.ClickSend.problems(200, ok), [])
        bad_number = {"http_code": 200, "response_code": "SUCCESS",
                      "data": {"messages": [{"to": "+15555550123", "status": "INVALID_RECIPIENT"}]}}
        self.assertEqual(sms.ClickSend.problems(200, bad_number), ["...0123: INVALID_RECIPIENT"])
        unauth = {"http_code": 401, "response_code": "UNAUTHORIZED", "response_msg": "Invalid credentials"}
        self.assertEqual(sms.ClickSend.problems(401, unauth), ["UNAUTHORIZED: Invalid credentials"])
        self.assertEqual(sms.ClickSend.problems(502, None), ["HTTP 502"])


class AlerterSendsTexts(unittest.TestCase):
    """End to end through Alerter.run: what gets texted, and quiet mode."""

    class Quiet:
        def __init__(self, on):
            self.on = on

        def active(self, now=None):
            return self.on

    def run_events(self, events, quiet=False):
        cfg = jarvis.load_config(ROOT / "config.example.yaml")
        cfg["alerts"] = [
            {"name": "drone", "kind": "scanner_drone", "messages": ["A {model} drone is nearby."], "cooldown_minutes": 5},
            {"name": "smoke", "kind": "alarm", "urgent": True, "messages": ["Smoke: {device}."]},
            {"name": "door", "kind": "detection", "sms": False, "messages": ["Someone at the door."]},
        ]
        alerter = jarvis.Alerter(cfg)
        client = FakeClient()
        alerter.sms = sms.SmsRelay({"enabled": True, "to": ["+15555550123"]}, "Jarvis", client=client)

        async def nothing(*a, **k):
            return None
        alerter.prewarm = nothing
        alerter._speak = nothing

        async def scenario():
            task = asyncio.ensure_future(alerter.run())
            while not alerter.ready:
                await asyncio.sleep(0.01)
            for ev in events:
                alerter.submit(ev)
            await asyncio.sleep(0.1)
            task.cancel()

        saved = (jarvis.JOURNAL, jarvis.QUIET)
        jarvis.JOURNAL, jarvis.QUIET = None, self.Quiet(quiet)
        try:
            asyncio.run(scenario())
        finally:
            jarvis.JOURNAL, jarvis.QUIET = saved
        return [body for _, body in client.sent]

    EVENTS = [
        {"kind": "scanner_drone", "model": "DJI"},
        {"kind": "scanner_drone", "model": "DJI"},                     # rule cooldown: once
        {"kind": "detection", "camera_name": "Front Door"},             # rule has sms: false
        {"kind": "say", "text": "Dinner is ready."},                    # say: not texted by default
        {"kind": "condition", "rule": "garage-left-open", "text": "The garage has been open for 15 minutes."},
        {"kind": "alarm", "device_name": "Hallway"},
    ]

    def test_texts_alerts_and_conditions(self):
        self.assertEqual(self.run_events(self.EVENTS), [
            "Jarvis: A DJI drone is nearby.",
            "Jarvis: The garage has been open for 15 minutes.",
            "Jarvis: Smoke: Hallway.",
        ])

    def test_quiet_mode_texts_only_urgent(self):
        self.assertEqual(self.run_events(self.EVENTS, quiet=True), ["Jarvis: Smoke: Hallway."])


if __name__ == "__main__":
    unittest.main()
