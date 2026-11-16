"""Source watchdog: notice when a data source goes quiet, and when it comes back.

Each source in `watchdog.sources` names where its freshness comes from:

  mqtt: <topic pattern>          newest observation among matching home-broker topics
  scanner: rf|rid|wifi|rc900|any newest message from the RF scanner box's broker
  airspace_sensor: <sensor id>   last_observation_at of an airspace-server sensor
  health: <journal health name>  last time that health record read "ok"

A source is down once nothing fresh arrived for `quiet_minutes`, and back once
it is fresh again (within half that). Sources whose channel is itself down
(home broker disconnected, scanner broker disconnected, airspace API failing)
are not judged, so one broken connection is one alert, not one per sensor.
Outages starting in the same check are announced together (kind source_quiet),
recoveries likewise (kind source_back). State survives restarts, and nothing is
judged during the first `startup_grace_minutes`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("jarvis.watchdog")


def _join(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


class SourceWatchdog:
    def __init__(self, cfg: dict, journal, mqtt=None, scanner=None,
                 topic_matches: Optional[Callable[[str, str], bool]] = None):
        wcfg = cfg.get("watchdog") or {}
        self.journal = journal
        self.mqtt = mqtt
        self.scanner = scanner
        self.topic_matches = topic_matches
        self.check_s = float(wcfg.get("check_seconds", 60))
        self.grace_s = 60 * float(wcfg.get("startup_grace_minutes", 10))
        self.airspace_every = float(wcfg.get("airspace_poll_seconds", 300))
        self.sources = []
        for s in wcfg.get("sources", []):
            if s.get("enabled", True) is False:
                continue
            kind = next((k for k in ("mqtt", "scanner", "airspace_sensor", "health") if s.get(k)), None)
            if kind is None:
                log.warning("watchdog source %r has no mqtt/scanner/airspace_sensor/health key", s.get("name"))
                continue
            self.sources.append(dict(s, kind=kind, ref=s[kind], quiet_s=60 * float(s.get("quiet_minutes", 30)),
                                     spoken=s.get("spoken") or "the " + s["name"]))
        self.started = time.time()
        saved = (journal.get("watchdog:state") if journal else None) or {}
        self.state: Dict[str, dict] = {s["name"]: saved.get(s["name"], {}) for s in self.sources}
        self.listeners: List = []
        self.airspace: Dict[str, float] = {}      # sensor id -> last observation epoch
        self.airspace_ok_at = 0.0
        self.airspace_polled = 0.0

    # --- freshness per kind ------------------------------------------------------
    def _fresh_at(self, s: dict, now: float) -> Optional[float]:
        """Epoch of the source's newest data, or None when it can't be judged right now."""
        kind, ref = s["kind"], s["ref"]
        if kind == "mqtt":
            m = self.mqtt
            if not (m and m.client and m.client.is_connected()) or not self.topic_matches:
                return None
            with m.lock:
                times = [v[1].timestamp() for t, v in m.values.items() if self.topic_matches(ref, t)]
            return max(times, default=0.0)
        if kind == "scanner":
            sc = self.scanner
            if not (sc and sc.connected):
                return None
            return sc.last_seen(ref)
        if kind == "airspace_sensor":
            if now - self.airspace_ok_at > 3 * self.airspace_every:
                return None
            return self.airspace.get(ref, 0.0)
        if kind == "health":
            row = next((r for r in self.journal.health_rows() if r["name"] == ref), None) if self.journal else None
            st = self.state[s["name"]]
            if row and row.get("status") == "ok":
                st["ok_at"] = row["at"]
            return st.get("ok_at") or self.started
        return None

    async def _poll_airspace(self, session) -> None:
        url = os.environ.get("AIRSPACE_URL", "").rstrip("/")
        key = os.environ.get("AIRSPACE_API_KEY", "")
        if not (url and key) or not any(s["kind"] == "airspace_sensor" for s in self.sources):
            return
        try:
            async with session.get(url + "/api/sensors", headers={"X-API-Key": key}) as r:
                r.raise_for_status()
                rows = (await r.json()).get("sensors", [])
            for row in rows:
                at = row.get("last_observation_at")
                if at:
                    self.airspace[row["sensor_id"]] = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
            self.airspace_ok_at = time.time()
        except Exception as e:
            log.warning("watchdog airspace poll failed: %s", e)

    # --- evaluation --------------------------------------------------------------------
    def check(self, now: Optional[float] = None) -> List[dict]:
        now = time.time() if now is None else now
        went_down, came_back = [], []
        for s in self.sources:
            fresh = self._fresh_at(s, now)
            if fresh is None:
                continue
            age = max(0.0, now - fresh)
            st = self.state.setdefault(s["name"], {})
            if s["kind"] != "health" and self.journal:
                warming = now - self.started < self.grace_s
                status = "ok" if age <= s["quiet_s"] else "warming" if warming else "error"
                detail = "no data yet" if fresh <= 0 else "last data %d min ago" % (age // 60)
                self.journal.health(s["name"], status, detail, self.check_s * 3)
            if now - self.started < self.grace_s:
                continue
            if not st.get("down") and age > s["quiet_s"]:
                st.update(down=True, since=fresh)
                went_down.append((s, age))
            elif st.get("down") and age <= s["quiet_s"] / 2:
                outage = now - (st.get("since") or now)
                st.update(down=False, since=None)
                came_back.append((s, outage))
        events = []
        if went_down:
            names = [s["spoken"] for s, _ in went_down]
            minutes = int(round(max(a for _, a in went_down) / 60))
            verb = "has" if len(names) == 1 else "have"
            events.append({"kind": "source_quiet", "device": "+".join(s["name"] for s, _ in went_down),
                           "sources": ", ".join(s["name"] for s, _ in went_down), "minutes": str(minutes),
                           "detail": "%s %s gone quiet; nothing for %d minutes." % (_cap(_join(names)), verb, minutes)})
        if came_back:
            names = [s["spoken"] for s, _ in came_back]
            minutes = int(round(max(o for _, o in came_back) / 60))
            verb = "is" if len(names) == 1 else "are"
            events.append({"kind": "source_back", "device": "+".join(s["name"] for s, _ in came_back),
                           "sources": ", ".join(s["name"] for s, _ in came_back), "minutes": str(minutes),
                           "detail": "%s %s reporting again, after about %d minutes." % (_cap(_join(names)), verb, minutes)})
        if (went_down or came_back) and self.journal:
            self.journal.put("watchdog:state", self.state)
        return events

    def down_lines(self) -> List[str]:
        """For briefings: sources currently not reporting."""
        out = []
        for s in self.sources:
            st = self.state.get(s["name"]) or {}
            if st.get("down"):
                since = st.get("since")
                when = dt.datetime.fromtimestamp(since).strftime("%-I:%M %p") if since else "a while"
                out.append("%s (no data since %s)" % (s["spoken"], when))
        return ["- Data sources not reporting: " + "; ".join(out)] if out else []

    async def run(self) -> None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while True:
                try:
                    if time.time() - self.airspace_polled >= self.airspace_every:
                        self.airspace_polled = time.time()
                        await self._poll_airspace(session)
                    for ev in self.check():
                        log.info("event: %s", ev)
                        for listener in self.listeners:
                            listener(ev)
                except Exception:
                    log.exception("watchdog check failed")
                await asyncio.sleep(self.check_s)
