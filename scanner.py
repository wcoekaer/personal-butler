"""RF scanner box feed: drone detections from the scanner's own MQTT broker.

The scanner box (Oracle-RF-Server `scanner/`) publishes to its own broker
(SCANNER_MQTT_HOST, SCANNER_MQTT_PORT, SCANNER_MQTT_USERNAME, SCANNER_MQTT_PASSWORD):

  rid/drones      Bluetooth/Wi-Fi Remote ID decoded by rid_capture
  /scanner/wifi   Wi-Fi drone candidates, incl. OpenDroneID beacons (wifi_remote_id)
  /scanner/rf     HackRF 2.4/5.8 GHz sweep: active emitter tracks, peaks with verdict tiers
  drone_scan      RTL-SDR 900 MHz sweep

A decoded Remote ID within `max_miles` of the house is announced (kind
scanner_drone) and added to the airspace sightings, so the HUD radar, briefings
and weekly recap include it. RF-only and Wi-Fi-signature verdicts are not proof
of a drone: they are shown on the HUD and journaled (kind scanner_rf_drone) but
not spoken unless a rule for that kind is added.

Serials in airspace.ignore_serials (your own drone) are never announced, shown or
counted; while one of them is in the air, RF-only and Wi-Fi verdicts are
attributed to it and hidden as well. scanner.ignore_ouis hides Wi-Fi signature
candidates by vendor prefix.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger("jarvis.scanner")

# ANSI/CTA-2063-A serials start with the manufacturer code
MAKERS = {"1581": "DJI", "1668": "Skydio"}


def _miles(lat1, lng1, lat2, lng2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_word(lat1, lng1, lat2, lng2):
    y = math.sin(math.radians(lng2 - lng1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lng2 - lng1)))
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][int((deg + 22.5) // 45) % 8]


class DroneRegistry:
    """Shared across drone sources (house Remote ID sensor, scanner box) so one
    flight is announced once, by whichever source hears it first."""

    def __init__(self):
        self.lock = threading.Lock()
        self.activity: Dict[str, float] = {}

    def observe(self, serial: str, ts: float, gap_s: float) -> bool:
        """Record activity; True if the serial had no activity within gap_s before ts."""
        key = str(serial).upper()
        with self.lock:
            prev = self.activity.get(key)
            if prev is None or ts > prev:
                self.activity[key] = ts
            cutoff = ts - 7 * 86400
            if len(self.activity) > 500:
                self.activity = {k: v for k, v in self.activity.items() if v >= cutoff}
        return prev is None or ts - prev > gap_s


class ScannerFeed:
    def __init__(self, cfg: dict, home, registry: DroneRegistry, monitor=None):
        scfg = cfg.get("scanner") or {}
        acfg = cfg.get("airspace") or {}
        self.cfg = scfg
        self.home = home                                   # (lat, lng)
        self.registry = registry
        self.monitor = monitor                             # AirspaceMonitor: shared sightings list
        self.ignore_serials = {s.upper() for s in acfg.get("ignore_serials", [])}
        self.ignore_ouis = {o.upper() for o in scfg.get("ignore_ouis", [])}
        self.max_miles = float(scfg.get("max_miles", 2.0))
        self.gap_s = 60 * float(acfg.get("new_sighting_gap_minutes", 15))
        self.ignore_hold_s = 60 * float(scfg.get("own_drone_hold_minutes", 10))
        self.topics = {"rid": scfg.get("rid_topic", "rid/drones"),
                       "wifi": scfg.get("wifi_topic", "/scanner/wifi"),
                       "rf": scfg.get("rf_topic", "/scanner/rf"),
                       "rc900": scfg.get("rc900_topic", "drone_scan")}
        self.listeners: List = []
        self.lock = threading.Lock()
        self.client = None
        self.connected = False
        self.last_msg: Dict[str, float] = {}               # topic key -> epoch of last message
        self.rf: dict = {}
        self.wifi: dict = {}
        self.detections = collections.deque(maxlen=100)     # drone-like detections, never own drone
        self.own_until = 0.0                               # own drone in the air until (epoch)
        self.verdict_seen: Dict[str, float] = {}           # rate limit for journaled RF verdicts

    # --- connection -----------------------------------------------------------
    def start(self) -> bool:
        host = os.environ.get("SCANNER_MQTT_HOST")
        if not host:
            return False
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-scanner-%d" % os.getpid())
        user = os.environ.get("SCANNER_MQTT_USERNAME")
        if user:
            c.username_pw_set(user, os.environ.get("SCANNER_MQTT_PASSWORD"))
        if os.environ.get("SCANNER_MQTT_TLS") == "1":
            c.tls_set()
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 60)
        c.connect_async(host, int(os.environ.get("SCANNER_MQTT_PORT", 1883)), keepalive=60)
        c.loop_start()
        self.client = c
        return True

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("scanner MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        for t in self.topics.values():
            client.subscribe(t)
        log.info("scanner MQTT connected; subscribed to %s", ", ".join(self.topics.values()))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("scanner MQTT disconnected: %s", reason_code)

    def last_seen(self, key: str) -> float:
        with self.lock:
            if key == "any":
                return max(self.last_msg.values(), default=0.0)
            return self.last_msg.get(key, 0.0)

    # --- messages ---------------------------------------------------------------
    def _on_message(self, client, userdata, msg):
        key = next((k for k, t in self.topics.items() if t == msg.topic), None)
        if key is None or msg.retain:
            return
        now = time.time()
        with self.lock:
            self.last_msg[key] = now
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        try:
            if key == "rid":
                self._remote_id(data, now, "Remote ID", data.get("rssi"))
            elif key == "wifi":
                self._wifi(data, now)
            else:
                self._sweep(key, data, now)
        except Exception:
            log.exception("scanner message on %s failed", msg.topic)

    def _own(self, uas_id: str) -> bool:
        return str(uas_id).upper() in self.ignore_serials

    def _remote_id(self, d: dict, now: float, via: str, rssi=None) -> None:
        uas_id = str(d.get("uas_id") or "").strip()
        if not uas_id:
            return
        if self._own(uas_id):
            self.own_until = now + self.ignore_hold_s
            return
        lat, lon = d.get("lat"), d.get("lon")
        miles = bearing = None
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon) and self.home:
            miles = _miles(self.home[0], self.home[1], lat, lon)
            bearing = _bearing_word(self.home[0], self.home[1], lat, lon) if miles >= 0.05 else None
        if miles is not None and miles > self.max_miles:
            return
        maker = MAKERS.get(uas_id[:4], "")
        with self.lock:
            self.detections.append({"at": now, "kind": "rid", "id": uas_id, "via": via,
                                    "what": "%s drone · %s" % (maker or "Unidentified", via),
                                    "miles": miles, "dir": bearing, "rssi": rssi})
        if not self.registry.observe(uas_id, now, self.gap_s):
            return                                         # this flight was already announced
        if miles is None or miles < 0.05:
            where = "close to the house"
        else:
            where = "about %.1f miles to the %s" % (miles, bearing) if bearing else "about %.1f miles away" % miles
        model = maker or "unidentified"
        ev = {"kind": "scanner_drone", "device": uas_id, "serial": uas_id, "model": model,
              "drone": ("a %s drone" % maker) if maker else "an unidentified drone",
              "via": via, "where": where, "miles": miles, "dir": bearing or "",
              "distance": ("%.1f miles" % miles) if miles is not None else "an unknown distance",
              "near": "the house"}
        if self.monitor is not None:
            import datetime as dt
            self.monitor.sightings.append(dict(ev, kind="drone", source="scanner",
                                               at=dt.datetime.now(dt.timezone.utc).isoformat()))
        log.info("event: %s", ev)
        for listener in self.listeners:
            listener(ev)

    def _wifi(self, d: dict, now: float) -> None:
        cands = d.get("candidates") or []
        with self.lock:
            self.wifi = {"at": now, "networks": d.get("n_networks"), "candidates": len(cands)}
        for c in cands:
            rid = c.get("remote_id") or {}
            if c.get("classification") == "wifi_remote_id" and rid.get("uas_id"):
                self._remote_id(rid, now, "Wi-Fi Remote ID", c.get("rssi_dbm"))
            elif c.get("is_drone_candidate") and int(c.get("confidence") or 0) >= 70:
                if now < self.own_until or self.ignore_ouis & {o.upper() for o in c.get("ouis") or []}:
                    continue
                label = (c.get("classification") or "drone Wi-Fi").replace("_", " ")
                self._verdict("wifi", "Wi-Fi: " + label, c.get("bssid") or c.get("key_mac") or "", now)

    def _sweep(self, key: str, d: dict, now: float) -> None:
        if key == "rf":
            with self.lock:
                self.rf = {"at": now, "tracks": d.get("active_tracks"),
                           "suspect": bool(d.get("any_suspect_drone"))}
        if not d.get("any_drone_detected") or now < self.own_until:
            return
        for p in d.get("peaks") or d.get("regions") or []:
            verdict = str(p.get("verdict") or "")
            if p.get("tier") not in ("confirmed", "probable") and not verdict.startswith("DRONE"):
                continue
            fc = p.get("fc") or p.get("fc_hz") or p.get("center_hz")
            label = verdict.split(":", 1)[-1].strip() or "drone RF signature"
            band = ("%.0f MHz" % (fc / 1e6)) if isinstance(fc, (int, float)) else ""
            self._verdict(key, label + (" · " + band if band else ""), "%s:%s" % (key, round((fc or 0) / 5e6)), now)

    def _verdict(self, kind: str, what: str, ident: str, now: float) -> None:
        with self.lock:
            self.detections.append({"at": now, "kind": kind, "id": ident, "what": what})
            last = self.verdict_seen.get(ident, 0.0)
            if now - last < 600:
                return
            self.verdict_seen[ident] = now
        ev = {"kind": "scanner_rf_drone", "device": ident, "detail": what, "source": kind}
        log.info("event: %s", ev)
        for listener in self.listeners:
            listener(ev)

    # --- HUD -----------------------------------------------------------------------
    def hud_state(self, now: Optional[float] = None) -> dict:
        import datetime as dt

        now = time.time() if now is None else now
        with self.lock:
            rf_age = now - self.last_msg.get("rf", 0.0)
            hour = [x for x in self.detections if now - x["at"] <= 3600]
            last = self.detections[-1] if self.detections else None
            rf = dict(self.rf)
        out = {"online": bool(self.connected and rf_age < 300), "tracks": rf.get("tracks"),
               "rid_hour": len({x["id"] for x in hour if x["kind"] == "rid"}),
               "signatures_hour": len({x["id"] for x in hour if x["kind"] != "rid"})}
        if last and now - last["at"] <= 6 * 3600:
            out["last"] = {"what": last["what"], "kind": last["kind"],
                           "at": dt.datetime.fromtimestamp(last["at"]).strftime("%-I:%M %p"),
                           "recent": now - last["at"] <= 900,
                           "miles": round(last["miles"], 2) if last.get("miles") is not None else None,
                           "dir": last.get("dir")}
        return out
