import json
import time
import unittest
from types import SimpleNamespace

import jarvis
import watchdog


class HomeKitMeasurements(unittest.TestCase):
    def events(self, characteristic, text):
        m = jarvis.MqttState({"topics": []})
        got = []
        m.listeners.append(got.append)
        payload = json.dumps({"accessory": "Sensor", "room": "Living Room", "characteristic": characteristic, "text": text})
        m._on_message(None, None, SimpleNamespace(topic="homekit/events", payload=payload.encode(), retain=False))
        return [e for e in got if e["kind"] == "homekit"]

    def test_measurements_are_not_events_states_are(self):
        for char in ("Volatile Organic Compound Density", "PM2.5 Density", "Carbon dioxide Level",
                     "Current Temperature", "Current Relative Humidity", "Air Quality"):
            self.assertEqual(self.events(char, "9"), [], char)
        for char in ("Contact Sensor State", "Smoke Detected", "Carbon Monoxide Detected", "Leak Detected", "Lock Current State"):
            self.assertEqual(len(self.events(char, "open")), 1, char)


class FakeJournal:
    def __init__(self):
        self.rows = {}

    def get(self, key, default=None):
        return default

    def put(self, key, data):
        pass

    def health(self, name, status, detail="", ttl=300, now=None):
        self.rows[name] = (status, detail)

    def health_rows(self, now=None):
        return []


class WatchdogWarmup(unittest.TestCase):
    def test_unseen_source_is_warming_during_grace_then_error(self):
        j = FakeJournal()
        sc = SimpleNamespace(connected=True, last_seen=lambda key: 0.0)
        wd = watchdog.SourceWatchdog({"watchdog": {"startup_grace_minutes": 10, "sources": [
            {"name": "RF scanner", "scanner": "rf", "quiet_minutes": 10}]}}, j, scanner=sc)
        self.assertEqual(wd.check(), [])
        self.assertEqual(j.rows["RF scanner"], ("warming", "no data yet"))
        wd.started -= 3600
        events = wd.check()
        self.assertEqual(j.rows["RF scanner"], ("error", "no data yet"))
        self.assertEqual([e["kind"] for e in events], ["source_quiet"])


if __name__ == "__main__":
    unittest.main()
