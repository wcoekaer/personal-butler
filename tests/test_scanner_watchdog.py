import datetime as dt
import json
import threading
import time
import unittest
from types import SimpleNamespace

import jarvis
import scanner
import watchdog

HOME = (37.0000, -122.0000)
OWN = "1668ZZ00TEST0001"
CFG = {"airspace": {"ignore_serials": [OWN], "new_sighting_gap_minutes": 15},
       "scanner": {"max_miles": 2.0, "ignore_ouis": ["38:1D:14"]}}


def msg(topic, payload, retain=False):
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode(), retain=retain)


class ScannerFeedTests(unittest.TestCase):
    def setUp(self):
        self.registry = scanner.DroneRegistry()
        self.monitor = SimpleNamespace(sightings=[])
        self.feed = scanner.ScannerFeed(CFG, HOME, self.registry, monitor=self.monitor)
        self.feed.connected = True
        self.events = []
        self.feed.listeners.append(self.events.append)

    def send(self, topic, payload):
        self.feed._on_message(None, None, msg(topic, payload))

    def test_remote_id_announced_once_per_flight(self):
        rid = {"uas_id": "1581F4XFC23400D1", "lat": 37.0025, "lon": -122.0000, "rssi": -70}
        self.send("rid/drones", rid)
        self.send("rid/drones", rid)
        self.assertEqual([e["kind"] for e in self.events], ["scanner_drone"])
        ev = self.events[0]
        self.assertEqual((ev["drone"], ev["device"]), ("a DJI drone", "1581F4XFC23400D1"))
        self.assertIn("miles to the north", ev["where"])
        self.assertEqual(self.monitor.sightings[0]["source"], "scanner")

    def test_own_drone_is_never_announced_shown_or_counted(self):
        self.send("rid/drones", {"uas_id": OWN, "lat": 37.0001, "lon": -121.9998})
        self.send("/scanner/rf", {"active_tracks": 40, "any_drone_detected": True,
                                  "peaks": [{"tier": "confirmed", "verdict": "DRONE: OcuSync", "fc": 5.6e9}]})
        self.send("/scanner/wifi", {"n_networks": 30, "candidates": [
            {"classification": "wifi_remote_id", "remote_id": {"uas_id": OWN}},
            {"classification": "wifi_drone_oui", "is_drone_candidate": True, "confidence": 90, "ouis": ["AA:BB:CC"]}]})
        self.assertEqual(self.events, [])
        self.assertEqual(list(self.feed.detections), [])
        self.assertEqual(self.monitor.sightings, [])
        self.assertEqual(self.feed.hud_state()["rid_hour"], 0)

    def test_drone_already_announced_by_the_house_sensor_is_not_repeated(self):
        self.registry.observe("1581F4XFC23400D1", time.time() - 60, 900)
        self.send("rid/drones", {"uas_id": "1581F4XFC23400D1"})
        self.assertEqual(self.events, [])
        self.assertEqual(self.feed.hud_state()["rid_hour"], 1)

    def test_wifi_remote_id_and_signature_candidates(self):
        self.send("/scanner/wifi", {"n_networks": 30, "candidates": [
            {"classification": "wifi_remote_id", "rssi_dbm": -60,
             "remote_id": {"uas_id": "ABCD1234", "lat": 37.0005, "lon": -121.9992}},
            {"classification": "wifi_drone_oui", "is_drone_candidate": True, "confidence": 90, "ouis": ["38:1D:14"]},
            {"classification": "wifi_drone_ssid", "is_drone_candidate": True, "confidence": 80, "bssid": "aa:bb"}]})
        kinds = [(e["kind"], e.get("via") or e.get("detail")) for e in self.events]
        self.assertEqual(kinds, [("scanner_drone", "Wi-Fi Remote ID"), ("scanner_rf_drone", "Wi-Fi: wifi drone ssid")])
        self.assertEqual(self.events[0]["drone"], "an unidentified drone")

    def test_far_drone_ignored_and_rf_verdict_rate_limited(self):
        self.send("rid/drones", {"uas_id": "1581FAR", "lat": 37.15, "lon": -122.00})
        rf = {"active_tracks": 41, "any_drone_detected": True,
              "peaks": [{"tier": "probable", "verdict": "DRONE: analog FPV video", "fc": 5.8e9}]}
        self.send("/scanner/rf", rf)
        self.send("/scanner/rf", rf)
        self.assertEqual([e["kind"] for e in self.events], ["scanner_rf_drone"])
        hud = self.feed.hud_state()
        self.assertTrue(hud["online"] and hud["tracks"] == 41 and hud["last"]["recent"])
        self.assertEqual(hud["last"]["what"], "analog FPV video · 5800 MHz")


class FakeJournal:
    def __init__(self):
        self.kv, self.rows = {}, {}

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def put(self, key, data):
        self.kv[key] = json.loads(json.dumps(data))

    def health(self, name, status, detail="", ttl=300, now=None):
        self.rows[name] = {"name": name, "status": status, "at": time.time() if now is None else now}

    def health_rows(self, now=None):
        return list(self.rows.values())


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.connected = True
        self.mqtt = SimpleNamespace(lock=threading.Lock(), values={},
                                    client=SimpleNamespace(is_connected=lambda: self.connected))
        self.journal = FakeJournal()
        cfg = {"watchdog": {"startup_grace_minutes": 0, "sources": [
            {"name": "Weather station", "spoken": "the weather station", "mqtt": "http_weatherflow", "quiet_minutes": 30},
            {"name": "Powerpack", "mqtt": "/tesla/powerpack/state", "quiet_minutes": 15},
            {"name": "DELTA Pro data", "health": "ecoflow", "quiet_minutes": 20}]}}
        self.wd = watchdog.SourceWatchdog(cfg, self.journal, mqtt=self.mqtt, topic_matches=jarvis.topic_matches)
        self.now = time.time()

    def seen(self, topic, minutes_ago):
        self.mqtt.values[topic] = ("{}", dt.datetime.fromtimestamp(self.now - 60 * minutes_ago))

    def test_quiet_sources_grouped_then_back(self):
        self.seen("http_weatherflow", 45)
        self.seen("/tesla/powerpack/state", 20)
        self.journal.health("ecoflow", "ok", now=self.now)
        events = self.wd.check(self.now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "source_quiet")
        self.assertEqual(events[0]["detail"], "The weather station and the Powerpack have gone quiet; nothing for 45 minutes.")
        self.assertEqual(self.wd.check(self.now + 60), [])                 # once per outage
        self.assertIn("the weather station", self.wd.down_lines()[0])
        self.seen("http_weatherflow", 0)
        back = self.wd.check(self.now)
        self.assertEqual([e["kind"] for e in back], ["source_back"])
        self.assertTrue(back[0]["detail"].startswith("The weather station is reporting again"))

    def test_disconnected_broker_judges_nothing(self):
        self.seen("http_weatherflow", 90)
        self.connected = False
        self.assertEqual([e for e in self.wd.check(self.now) if e["kind"] == "source_quiet"
                          and "weather" in e["detail"]], [])

    def test_health_source_and_restart_state(self):
        self.seen("http_weatherflow", 1)
        self.seen("/tesla/powerpack/state", 1)
        self.journal.health("ecoflow", "error", now=self.now)
        self.wd.started = self.now - 3600
        self.wd.state["DELTA Pro data"]["ok_at"] = self.now - 25 * 60
        events = self.wd.check(self.now)
        self.assertEqual(events[0]["detail"], "The DELTA Pro data has gone quiet; nothing for 25 minutes.")
        again = watchdog.SourceWatchdog({"watchdog": {"startup_grace_minutes": 0, "sources": [
            {"name": "DELTA Pro data", "health": "ecoflow", "quiet_minutes": 20}]}}, self.journal)
        self.assertEqual(again.check(self.now + 60), [])                     # restart: no re-announce


if __name__ == "__main__":
    unittest.main()
