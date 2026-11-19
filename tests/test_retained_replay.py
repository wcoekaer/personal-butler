import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import jarvis
import watchdog

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "test/powerpack/state"
EPOCH = dt.datetime.fromtimestamp(0)


class Msg:
    """What paho hands to on_message."""

    def __init__(self, payload, retain=False, topic=TOPIC):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class RetainedReplay(unittest.TestCase):
    """The broker replays retained messages on every new subscription (the hourly
    discovery window, a reconnect). A replay has no time of its own.

    Regression: the replay reset the topic's last-seen time to 1970, so the watchdog
    announced "the Powerpack has gone quiet; nothing for 29916248 minutes" and, at the
    next live message, "reporting again"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = jarvis.load_config(ROOT / "config.example.yaml")["mqtt"]
        off = {"enabled": False}
        self.m = jarvis.MqttState(dict(
            cfg, persist=False, discover=False, topics=[{"topic": TOPIC}], unifi=off, internet=off, energy=off,
            cache_file=str(Path(self.tmp.name) / "mqtt.json"), catalog_file=str(Path(self.tmp.name) / "topics.json")))

    def deliver(self, payload, retain=False):
        self.m._on_message(None, None, Msg(payload, retain))
        return self.m.values[TOPIC]

    def test_replay_of_a_message_already_seen_keeps_its_time(self):
        _, live = self.deliver('{"soc": 74}')
        self.assertGreater(live, dt.datetime.now() - dt.timedelta(seconds=5))
        value, seen = self.deliver('{"soc": 74}', retain=True)
        self.assertEqual((value, seen), ('{"soc": 74}', live))

    def test_replay_with_a_newer_payload_updates_the_value_but_never_goes_back_in_time(self):
        _, live = self.deliver('{"soc": 74}')
        value, seen = self.deliver('{"soc": 75}', retain=True)      # e.g. missed while reconnecting
        self.assertEqual(value, '{"soc": 75}')
        self.assertEqual(seen, live)

    def test_first_retained_message_still_has_unknown_age(self):
        _, seen = self.deliver('{"soc": 74}', retain=True)          # startup: could be days old
        self.assertEqual(seen, EPOCH)
        _, seen = self.deliver('{"soc": 74}')                       # first live message
        self.assertGreater(seen, dt.datetime.now() - dt.timedelta(seconds=5))

    def test_watchdog_sees_no_outage_across_a_replay(self):
        wd = watchdog.SourceWatchdog({"watchdog": {"startup_grace_minutes": 0, "sources": [
            {"name": "Powerpack", "mqtt": TOPIC, "quiet_minutes": 15}]}}, None, mqtt=self.m,
            topic_matches=jarvis.topic_matches)
        self.m.client = SimpleNamespace(is_connected=lambda: True)
        self.deliver('{"soc": 74}')
        self.deliver('{"soc": 74}', retain=True)
        self.assertEqual(wd.check(), [])


class NeverSeenSource(unittest.TestCase):
    """A source with no data at all has been silent since Jarvis started watching it,
    not since 1970: the announcement and the recorded start of the outage say so."""

    def test_minutes_count_from_when_watching_started(self):
        now = 1_800_000_000.0
        mqtt = SimpleNamespace(lock=threading.Lock(), values={TOPIC: ("{}", EPOCH)},
                               client=SimpleNamespace(is_connected=lambda: True))
        wd = watchdog.SourceWatchdog({"watchdog": {"startup_grace_minutes": 10, "sources": [
            {"name": "Powerpack", "mqtt": TOPIC, "quiet_minutes": 15}]}}, None, mqtt=mqtt,
            topic_matches=jarvis.topic_matches)
        wd.started = now - 20 * 60
        events = wd.check(now)
        self.assertEqual([e["detail"] for e in events], ["The Powerpack has gone quiet; nothing for 20 minutes."])
        self.assertEqual(wd.state["Powerpack"]["since"], wd.started)
        mqtt.values[TOPIC] = ("{}", dt.datetime.fromtimestamp(now + 60))
        back = wd.check(now + 60)
        self.assertEqual(back[0]["detail"], "The Powerpack is reporting again, after about 21 minutes.")


if __name__ == "__main__":
    unittest.main()
