"""More outside-world feeds (registered into feeds.FEEDS; each is opt-in: it only
runs when its section exists under `feeds:` in config.yaml).

  satellite_fires  NASA FIRMS satellite fire detections near the house (FIRMS_MAP_KEY)
  space_weather    NOAA SWPC geomagnetic storm level, now and forecast
  airports         FAA NAS status: ground stops / closures (and delay programs) at chosen airports
  launches         Launch Library 2: rocket launches from a site (Vandenberg), if visible from home
  mail_watch       Messages from chosen senders in the Mail inbox (via the JarvisContext helper):
                   county emergency alerts, delivery notices ...
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import feeds
from feeds import Feed, USER_AGENT, bearing_word, miles_between, parse_time

UTC = dt.timezone.utc
COMPASS16 = ["north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast",
             "southeast", "south-southeast", "south", "south-southwest", "southwest", "west-southwest",
             "west", "west-northwest", "northwest", "north-northwest"]


def compass16(lat1, lng1, lat2, lng2) -> str:
    y = math.sin(math.radians(lng2 - lng1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lng2 - lng1)))
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return COMPASS16[int((deg + 11.25) // 22.5) % 16]


def sun_altitude(lat: float, lng: float, when: dt.datetime) -> float:
    """Solar elevation in degrees (low-precision almanac formula, ~1 degree)."""
    d = (when.astimezone(UTC) - dt.datetime(2000, 1, 1, 12, tzinfo=UTC)).total_seconds() / 86400.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians((gmst * 15 + lng) % 360) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def hm(t: dt.datetime) -> str:
    return t.astimezone().strftime("%-I:%M %p")


class OptIn(Feed):
    opt_in = True
    requires_env: tuple = ()

    async def get_text(self, session, url: str, **kw) -> str:
        headers = dict(kw.pop("headers", {}), **{"User-Agent": USER_AGENT})
        async with session.get(url, headers=headers, **kw) as r:
            if r.status != 200:
                raise RuntimeError("%s HTTP %s" % (self.name, r.status))
            return await r.text()

    def hud_items(self, now: dt.datetime) -> List[dict]:
        return []


# --------------------------------------------------------------------------- NASA FIRMS


class SatelliteFires(OptIn):
    """New satellite fire detections (VIIRS / MODIS) near the house.

    One announcement per area: a new cluster within `alert_miles` is announced
    unless one was announced within `area_miles` in the last 12 hours, so an
    ongoing fire doesn't repeat on every satellite pass."""

    name = "satellite_fires"
    requires_env = ("FIRMS_MAP_KEY",)
    BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
    NAMES = {"N20": "NOAA-20", "N21": "NOAA-21", "N": "Suomi NPP", "Terra": "Terra", "Aqua": "Aqua",
             "T": "Terra", "A": "Aqua"}

    def _rows(self, text: str, source: str) -> List[dict]:
        out = []
        for r in csv.DictReader(io.StringIO(text)):
            try:
                lat, lng = float(r["latitude"]), float(r["longitude"])
                when = dt.datetime.strptime(r["acq_date"] + r["acq_time"].zfill(4), "%Y-%m-%d%H%M").replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            conf = str(r.get("confidence", "")).strip().lower()
            if conf in ("l", "low") or (conf.isdigit() and int(conf) < int(self.cfg.get("modis_min_confidence", 50))):
                continue
            out.append({"lat": lat, "lng": lng, "at": when.timestamp(), "source": source,
                        "sat": self.NAMES.get(r.get("satellite", ""), r.get("satellite", "")),
                        "instrument": r.get("instrument", ""), "frp": r.get("frp")})
        return out

    async def poll(self, session) -> None:
        key = os.environ["FIRMS_MAP_KEY"]
        watch = float(self.cfg.get("watch_miles", 25))
        dlat = watch / 69.0
        dlng = watch / (69.0 * math.cos(math.radians(self.home[0])))
        box = "%.4f,%.4f,%.4f,%.4f" % (self.home[1] - dlng, self.home[0] - dlat, self.home[1] + dlng, self.home[0] + dlat)
        rows = []
        for source in self.cfg.get("sources", ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]):
            text = await self.get_text(session, "%s/%s/%s/%s/1" % (self.BASE, key, source, box))
            if text.lstrip().lower().startswith(("invalid", "error")):
                raise RuntimeError("FIRMS rejected the request")
            rows.extend(self._rows(text, source))
        now = time.time()
        ignore = self.cfg.get("ignore_points", [])        # [[lat, lng, radius_miles], ...] e.g. known flares
        seen = self.state.setdefault("seen", {})
        fresh = []
        for r in rows:
            r["miles"] = round(self.distance(r["lat"], r["lng"]), 1)
            if r["miles"] > watch or any(miles_between(p[0], p[1], r["lat"], r["lng"]) <= p[2] for p in ignore):
                continue
            rid = "%s:%.3f,%.3f:%d" % (r["source"], r["lat"], r["lng"], r["at"])
            if rid not in seen:
                seen[rid] = r
                fresh.append(r)
        self.state["seen"] = {k: v for k, v in seen.items() if now - v["at"] < 3 * 86400}
        announced = [a for a in self.state.get("announced", []) if now - a["ts"] < 12 * 3600]
        alert = float(self.cfg.get("alert_miles", 10))
        area = float(self.cfg.get("area_miles", 3))
        for r in sorted((x for x in fresh if x["miles"] <= alert), key=lambda x: x["miles"]):
            if any(miles_between(a["lat"], a["lng"], r["lat"], r["lng"]) <= area for a in announced):
                continue
            near = [x for x in fresh if miles_between(x["lat"], x["lng"], r["lat"], r["lng"]) <= area]
            announced.append({"lat": r["lat"], "lng": r["lng"], "ts": now})
            self.emit({"kind": "satellite_fire", "device": "%.2f,%.2f" % (r["lat"], r["lng"]),
                       "distance": "%.1f miles" % r["miles"], "miles": r["miles"],
                       "direction": self.direction(r["lat"], r["lng"]), "count": str(len(near)),
                       "satellite": ("%s %s" % (r["sat"], r["instrument"])).strip(),
                       "when": hm(dt.datetime.fromtimestamp(r["at"], UTC))})
        self.state["announced"] = announced

    def recent(self, hours: float = 24) -> List[dict]:
        cut = time.time() - hours * 3600
        return sorted((r for r in self.state.get("seen", {}).values() if r["at"] >= cut), key=lambda r: r["miles"])

    def lines(self, now):
        rec = self.recent()
        if not rec:
            return []
        n = rec[0]
        return ["- Satellite fire detections within %s miles, last 24 hours: %d (nearest %.1f miles %s, %s)" % (
            self.cfg.get("watch_miles", 25), len(rec), n["miles"], self.direction(n["lat"], n["lng"]),
            hm(dt.datetime.fromtimestamp(n["at"], UTC)))]

    def hud_items(self, now):
        rec = self.recent()
        if not rec:
            return []
        n = rec[0]
        return [{"label": "Satellite heat · %d in 24 h" % len(rec),
                 "value": "%.1f mi %s" % (n["miles"], self.direction(n["lat"], n["lng"])),
                 "level": "warn" if n["miles"] <= float(self.cfg.get("alert_miles", 10)) else ""}]


# --------------------------------------------------------------------------- NOAA SWPC


class SpaceWeather(OptIn):
    """Geomagnetic storm level (NOAA G-scale) now and over the next three days."""

    name = "space_weather"
    URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
    WORDS = {1: "minor", 2: "moderate", 3: "strong", 4: "severe", 5: "extreme"}

    async def poll(self, session) -> None:
        d = await self.get_json(session, self.URL)
        level = int((d.get("0") or {}).get("G", {}).get("Scale") or 0)
        forecast = []
        for k in ("1", "2", "3"):
            day = d.get(k) or {}
            g = int(((day.get("G") or {}).get("Scale")) or 0)
            forecast.append({"date": day.get("DateStamp"), "g": g})
        now = time.time()
        st = self.state
        alert_g = int(self.cfg.get("alert_level", 3))
        aurora_g = int(self.cfg.get("aurora_level", 4))
        peak = int(st.get("storm_peak", 0))
        if level >= alert_g and level > peak:
            st["storm_peak"] = level
            self.emit({"kind": "space_weather_storm", "device": "G%d" % level, "level": "G%d" % level,
                       "strength": self.WORDS.get(level, ""),
                       "aurora": " Auroras may be visible to the north after dark." if level >= aurora_g else ""})
        if level == 0:
            if st.get("quiet_since") is None:
                st["quiet_since"] = now
            elif now - st["quiet_since"] > 12 * 3600:
                st["storm_peak"] = 0                     # storm over: the next one announces again
        else:
            st["quiet_since"] = None
        top = max(forecast, key=lambda f: f["g"]) if forecast else {"g": 0}
        if top["g"] >= alert_g and st.get("watch") != "%s:%d" % (top["date"], top["g"]):
            st["watch"] = "%s:%d" % (top["date"], top["g"])
            when = dt.datetime.strptime(top["date"], "%Y-%m-%d").strftime("%A") if top.get("date") else "the coming days"
            self.emit({"kind": "space_weather_watch", "device": st["watch"], "level": "G%d" % top["g"],
                       "strength": self.WORDS.get(top["g"], ""), "day": when})
        st.update(level=level, forecast=forecast)

    def lines(self, now):
        level = int(self.state.get("level") or 0)
        fc = [f for f in self.state.get("forecast", []) if f["g"] >= 2]
        out = []
        if level >= 1:
            out.append("- Space weather: geomagnetic storm G%d (%s) now%s" % (
                level, self.WORDS[level], "; Starlink and GPS may degrade" if level >= 3 else ""))
        for f in fc:
            out.append("- Space weather forecast: G%d (%s) storm expected %s" % (
                f["g"], self.WORDS[f["g"]], dt.datetime.strptime(f["date"], "%Y-%m-%d").strftime("%A")))
        return out

    def hud_items(self, now):
        level = int(self.state.get("level") or 0)
        if level < 1:
            return []
        return [{"label": "Geomagnetic storm", "value": "G%d %s" % (level, self.WORDS[level]),
                 "level": "warn" if level >= 3 else ""}]


# --------------------------------------------------------------------------- FAA NAS status


class AirportStatus(OptIn):
    """Ground stops and closures (announced) plus delay programs (briefings) at chosen airports."""

    name = "airports"
    URL = "https://nasstatus.faa.gov/api/airport-status-information"

    @staticmethod
    def parse(xml_text: str) -> Dict[str, List[dict]]:
        """{airport: [{"type": ground_stop|closure|ground_delay|delay, fields...}]}"""
        out: Dict[str, List[dict]] = {}
        root = ET.fromstring(xml_text)
        for dtype in root.iter("Delay_type"):
            name = (dtype.findtext("Name") or "").lower()
            kind = ("ground_stop" if "ground stop" in name else "closure" if "closure" in name
                    else "ground_delay" if "ground delay" in name else "delay")
            for el in dtype.iter():
                arpt = el.findtext("ARPT")
                if not arpt or el.find("ARPT") is None:
                    continue
                item = {"type": kind}
                for child in el:
                    if child.tag != "ARPT" and child.text and child.text.strip():
                        item[child.tag.lower()] = " ".join(child.text.split())
                out.setdefault(arpt.strip().upper(), []).append(item)
        return out

    @staticmethod
    def when_text(s: Optional[str]) -> str:
        """'Nov 21 at 14:15 UTC.' -> 'Friday 6:15 AM'; other formats pass through."""
        m = re.match(r"\s*([A-Za-z]{3}) (\d{1,2}) at (\d{1,2}):(\d{2}) UTC", s or "")
        if not m:
            return (s or "").strip().rstrip(".")
        now = dt.datetime.now(UTC)
        t = dt.datetime.strptime("%s %s %d %s:%s" % (m.group(1), m.group(2), now.year, m.group(3), m.group(4)),
                                 "%b %d %Y %H:%M").replace(tzinfo=UTC)
        if (t - now).days < -180:
            t = t.replace(year=now.year + 1)
        return t.astimezone().strftime("%A %-I:%M %p")

    async def poll(self, session) -> None:
        status = self.parse(await self.get_text(session, self.URL))
        names = dict({"SFO": "San Francisco", "OAK": "Oakland", "SJC": "San Jose"}, **(self.cfg.get("names") or {}))
        watched = [a.upper() for a in self.cfg.get("airports", ["SFO", "OAK", "SJC"])]
        now = time.time()
        known = self.state.get("active", {})
        active, recent = {}, self.state.get("announced", {})
        for code in watched:
            for item in status.get(code, []):
                if item["type"] not in ("ground_stop", "closure"):
                    continue
                key = "%s|%s|%s" % (code, item["type"], item.get("reason", ""))
                active[key] = item
                if key in known or now - recent.get(key, 0) < 7 * 86400:
                    continue
                recent[key] = now
                reason = item.get("reason", "") if item["type"] == "ground_stop" else ""
                ev = {"device": code, "code": code, "airport": names.get(code, code), "reason": reason,
                      "reason_text": (" because of " + reason.lower()) if reason else ""}
                if item["type"] == "ground_stop":
                    ev.update(kind="airport_ground_stop",
                              until=("until " + item["end_time"].lower()) if item.get("end_time") else "for now")
                else:
                    reopen = self.when_text(item.get("reopen"))
                    ev.update(kind="airport_closure", until=("until " + reopen) if reopen else "for now")
                self.emit(ev)
        self.state["active"] = active
        self.state["announced"] = {k: v for k, v in recent.items() if now - v < 7 * 86400}
        self.state["programs"] = {c: status.get(c, []) for c in watched if status.get(c)}

    def _describe(self, code: str, item: dict) -> str:
        t = item["type"]
        if t == "ground_stop":
            return "ground stop%s%s" % ((" (%s)" % item["reason"]) if item.get("reason") else "",
                                        (" until " + item["end_time"]) if item.get("end_time") else "")
        if t == "closure":
            return "closed until %s" % self.when_text(item.get("reopen"))
        if t == "ground_delay":
            return "ground delay program%s, average %s" % ((" (%s)" % item["reason"]) if item.get("reason") else "",
                                                           item.get("avg", "?"))
        return "delays%s" % ((" (%s)" % item["reason"]) if item.get("reason") else "")

    def lines(self, now):
        out = []
        for code, items in (self.state.get("programs") or {}).items():
            out.append("- %s airport: %s" % (code, "; ".join(self._describe(code, i) for i in items)))
        return out

    @staticmethod
    def _short(text: str) -> str:
        t = re.sub(r"(\d+) hours? and (\d+) minutes?", r"\1h\2m", text or "")
        return re.sub(r"(\d+) hours?", r"\1h", re.sub(r"(\d+) minutes?", r"\1m", t))

    def hud_items(self, now):
        out = []
        for code, items in (self.state.get("programs") or {}).items():
            for i in items:
                t = i["type"]
                value = ("ground stop" + (" · until " + i["end_time"] if i.get("end_time") else "") if t == "ground_stop"
                         else "closed · until " + self.when_text(i.get("reopen")) if t == "closure"
                         else "ground delay · avg " + self._short(i.get("avg", "?")) if t == "ground_delay"
                         else "delays" + (" · " + i["reason"] if i.get("reason") else ""))
                out.append({"label": code + " airport", "value": value[:40],
                            "level": "warn" if t in ("ground_stop", "closure") else ""})
        return out


# --------------------------------------------------------------------------- Launch Library 2


class RocketLaunches(OptIn):
    """Launches from a site (default Vandenberg, LL2 location 11) that should be
    visible from home: announced about 30 minutes ahead, listed in briefings."""

    name = "launches"
    URL = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/"

    def visibility(self, when: dt.datetime) -> Optional[str]:
        alt = sun_altitude(self.home[0], self.home[1], when)
        if alt > -2:
            return None                       # daylight: nothing to see
        return "twilight" if alt > -16 else "night"

    async def poll(self, session) -> None:
        loc = ",".join(str(x) for x in self.cfg.get("location_ids", [11]))
        d = await self.get_json(session, self.URL, params={"location__ids": loc, "limit": "6", "mode": "normal"})
        launches = []
        for x in d.get("results", []):
            net = parse_time(x.get("net"))
            pad = x.get("pad") or {}
            if not net or pad.get("latitude") is None:
                continue
            rocket, _, mission = str(x.get("name", "")).partition(" | ")
            plat, plng = float(pad["latitude"]), float(pad["longitude"])
            launches.append({"id": x.get("id"), "net": net.isoformat(), "rocket": rocket.replace(" Block 5", ""),
                             "mission": mission or "", "status": (x.get("status") or {}).get("abbrev", ""),
                             "precision": ((x.get("net_precision") or {}).get("abbrev") or ""),
                             "site": (pad.get("location") or {}).get("name", "").split(",")[0],
                             "direction": compass16(self.home[0], self.home[1], plat, plng),
                             "miles": round(self.distance(plat, plng))})
        self.state["launches"] = launches
        now = dt.datetime.now(UTC)
        lead = float(self.cfg.get("lead_minutes", 30))
        said = self.state.setdefault("announced", {})
        soonest = None
        for l in launches:
            net = parse_time(l["net"])
            mins = (net - now).total_seconds() / 60
            if mins > 0 and (soonest is None or mins < soonest):
                soonest = mins
            vis = self.visibility(net)
            key = "%s|%s" % (l["id"], l["net"])
            if (vis and l["status"] in ("Go", "TBC") and l["precision"] in ("SEC", "MIN", "HR", "")
                    and lead - 12 <= mins <= lead + 12 and key not in said):
                said[key] = time.time()
                self.emit({"kind": "rocket_launch", "device": l["id"] or key, "rocket": l["rocket"],
                           "mission": l["mission"], "site": l["site"], "time": hm(net),
                           "minutes": str(int(round(mins))), "direction": l["direction"],
                           "show": (" Launching in twilight, it may light up the sky with a glowing plume."
                                    if vis == "twilight" else "")})
        self.state["announced"] = {k: v for k, v in said.items() if time.time() - v < 7 * 86400}
        # LL2 free tier: 15 requests/hour. Every 2 h normally, every 10 min within 3 h of a launch.
        self.interval = 600.0 if soonest is not None and soonest < 180 else float(self.cfg.get("poll_seconds", 7200))

    def upcoming_visible(self, hours: float = 36) -> List[dict]:
        now = dt.datetime.now(UTC)
        out = []
        for l in self.state.get("launches", []):
            net = parse_time(l["net"])
            if net and dt.timedelta(0) < net - now <= dt.timedelta(hours=hours):
                vis = self.visibility(net)
                if vis:
                    out.append(dict(l, vis=vis, when=net))
        return out

    def lines(self, now):
        return ["- Rocket launch: %s%s from %s at %s, %s; should be visible low in the %s (%s launch, %d miles away)" % (
            l["rocket"], (" (%s)" % l["mission"]) if l["mission"] else "", l["site"],
            l["when"].astimezone().strftime("%A %-I:%M %p"), "go for launch" if l["status"] == "Go" else "time not final",
            l["direction"], l["vis"], l["miles"]) for l in self.upcoming_visible()]

    def hud_items(self, now):
        return [{"label": "Launch · " + l["rocket"], "value": "%s · %s" % (
            l["when"].astimezone().strftime("%a %-I:%M %p"), l["direction"]), "level": ""}
            for l in self.upcoming_visible()[:2]]


# --------------------------------------------------------------------------- Mail watch


class MailWatch(OptIn):
    """Inbox messages from chosen senders, exported by the JarvisContext helper
    (which only reads the senders listed in ~/Library/Caches/jarvis/mail-watch.json).

    Each rule: {name, sender (substring), subject (regex, optional), kind,
    urgent_words (regex alternatives that switch kind to <kind>_urgent)}.

    A message first seen more than `announce_within_minutes` (60) after Mail received it
    (Mail or the helper stalled) is not announced unless urgent; briefings still list it."""

    name = "mail_watch"
    CONTEXT = Path(os.path.expanduser("~/Library/Caches/jarvis/context.json"))
    WATCH = Path(os.path.expanduser("~/Library/Caches/jarvis/mail-watch.json"))
    WINDOW = re.compile(r"(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s*(?:-|–|to)\s*(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)")

    def __init__(self, cfg: dict, home):
        super().__init__(cfg, home)
        self.rules = [r for r in cfg.get("rules", []) if r.get("sender")]
        for v in (self.state.get("seen") or {}).values():          # keep nothing about unmatched messages
            if not v.get("source"):
                v.pop("subject", None)
                v.pop("received", None)
        senders = sorted({r["sender"] for r in self.rules})
        try:
            self.WATCH.parent.mkdir(parents=True, exist_ok=True)
            self.WATCH.write_text(json.dumps({"senders": senders, "days": int(cfg.get("days", 3)),
                                              "excerpt_chars": int(cfg.get("excerpt_chars", 1500)),
                                              "mailboxes": list(cfg.get("mailboxes", ["inbox", "trash"]))}))
        except OSError:
            pass

    def match(self, msg: dict) -> Optional[dict]:
        sender, subject = str(msg.get("sender", "")).lower(), str(msg.get("subject", ""))
        for r in self.rules:
            if r["sender"].lower() in sender and (not r.get("subject") or re.search(r["subject"], subject, re.I)):
                return r
        return None

    @staticmethod
    def clean_subject(subject: str, strip: Optional[str] = None) -> str:
        """Drop reply/forward and list prefixes ("Alert Message: ", "2026_"), and turn an
        all-caps subject into sentence case so it reads (and is spoken) naturally."""
        s = re.sub(r"^(re|fwd?):\s*", "", " ".join(str(subject).split()), flags=re.I)
        s = re.sub(strip or r"^(?:(?:alert|advisory|community) message:\s*|\d{4}_)", "", s, flags=re.I).strip(" *")
        s = re.sub(r"\*+", "", s).strip().rstrip(".").strip()
        if s and sum(c.isupper() for c in s) > 0.7 * sum(c.isalpha() for c in s):
            s = ". ".join(x[:1].upper() + x[1:] for x in s.lower().split(". "))
        return s

    MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
    DATE = re.compile(r"(?i)\b(until|through|thru|till|on|by|from|before|after|starting|beginning|ending|expires?)"
                      r"\s+(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![/\d])(?!\s*(?:inch|in\b|mile|of an?\b))")

    @classmethod
    def spoken_dates(cls, text: str) -> str:
        """'until 11/20' -> 'until November 20' (a British voice would otherwise guess at it);
        only after a date word, so '1/2 inch' stays as it is."""
        def say(m):
            year = int(m.group(4)) if m.group(4) else dt.date.today().year
            year += 2000 if year < 100 else 0
            try:
                d = dt.date(year, int(m.group(2)), int(m.group(3)))
            except ValueError:
                return m.group(0)
            return "%s %s %d%s" % (m.group(1), cls.MONTHS[d.month - 1], d.day,
                                   ", %d" % year if year != dt.date.today().year else "")
        return cls.DATE.sub(say, str(text))

    @staticmethod
    def norm(text: str) -> str:
        return re.sub(r"[\W_]+", "", str(text).lower())

    @staticmethod
    def repeats(sentence: str, subject: str) -> bool:
        """The sentence only says the subject again: the same words, a few of them, or all of
        them plus at most two more ("Road Closure Notice \u2013 Industrial Road, San Carlos" for
        "Road Closure Industrial Road, San Carlos")."""
        if MailWatch.norm(sentence) == MailWatch.norm(subject):
            return True
        said, subj = set(re.findall(r"\w+", sentence.lower())), set(re.findall(r"\w+", subject.lower()))
        if not said or len(subj) < 3:
            return False
        return said <= subj or (subj <= said and len(said - subj) <= 2)

    @staticmethod
    def join_lines(text: str) -> str:
        """The message on one line. A headline line ("Road Closure Notice \u2013 Industrial Road, San
        Carlos") gets a full stop instead of running into the next line's sentence: it is short,
        has no closing punctuation, ends on a capitalised word or a number, and the next line
        starts with one. A sentence wrapped across lines ends on a lowercase word: kept joined."""
        lines = [" ".join(l.split()) for l in str(text).replace("\ufffc", " ").splitlines()]
        lines = [l for l in lines if l]
        out = []
        for i, line in enumerate(lines):
            nxt = re.sub(r"^\W+", "", lines[i + 1])[:1] if i + 1 < len(lines) else ""
            last = re.sub(r"\W+$", "", line).rsplit(" ", 1)[-1][:1]
            if (len(line) <= 100 and not re.search(r"[.!?:;,\-\u2013\u2014]$", line)
                    and (last.isupper() or last.isdigit()) and (nxt.isupper() or nxt.isdigit())):
                line += "."
            out.append(line)
        return " ".join(out)

    @staticmethod
    def summarize(excerpt: str, skip: Optional[str] = None) -> str:
        """First sentence or two of a notice: no footer, list lead-in or agency headline,
        and not a sentence that only repeats the subject (`skip`)."""
        s = MailWatch.join_lines(excerpt)
        s = re.split(r"(?i)click the following link|unsubscribe|message sent via |view this (?:message|email) in", s)[0]
        s = re.sub(r"(?i)^.{0,240}?\bpowered by everbridge,? inc\.?\s*", "", s)  # "<Agency> is ... powered by Everbridge, Inc."
        s = re.sub(r"(?i)^[\w .'-]{0,30}?\balert[a-z]{0,4}\s*[:-]\s*", "", s)  # "SMC Alert: " / "SMC Alert - " / "ALERTSF: "
        s = re.sub(r"^.{0,80}?\b[A-Z][A-Z /&]{3,}\s+[-–]\s+", "", s)          # "<Agency> ROAD CLOSURE - "
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", s.strip()) if not skip or not MailWatch.repeats(x, skip)]
        out = ""
        for sentence in sentences[:2]:
            if out and len(out) + len(sentence) > 240:
                break
            out = (out + " " + sentence).strip()
        if len(out) > 240:                                                   # one long sentence: cut at a word
            out = out[:240].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        return out

    def event_for(self, rule: dict, msg: dict) -> dict:
        subject = " ".join(str(msg.get("subject", "")).split())
        raw = str(msg.get("excerpt", ""))                   # line breaks kept: they mark headlines
        excerpt = " ".join(raw.split())
        kind = rule.get("kind", "mail_notice")
        words = rule.get("urgent_words") or []
        if words and re.search("|".join(words), subject + " " + excerpt, re.I):
            kind += "_urgent"
        # dedupe: one announcement per cooldown for the whole rule (e.g. several UPS emails a morning)
        device = rule.get("name", "mail") if rule.get("dedupe") else str(msg.get("id", subject))[:80]
        clean = self.clean_subject(subject, rule.get("subject_strip"))
        detail = self.summarize(raw, skip=clean) if rule.get("summary", True) else ""
        # a bare subject ("Flood Advisory", or only the system's name): the body's opening sentence says it in full
        lead = re.split(r"(?<=[.!?])\s+", detail, 1)[0]
        if lead and (not self.norm(clean) or self.norm(lead).startswith(self.norm(clean))):
            clean = lead.rstrip(".")
            detail = self.summarize(raw, skip=clean)
        ev = {"kind": kind, "device": device, "source": rule.get("name", ""),
              "subject": self.spoken_dates(clean), "detail": self.spoken_dates(detail)}
        m = self.WINDOW.search(excerpt)
        ev["window"] = (", between %s and %s" % (m.group(1).upper().replace(".", ""), m.group(2).upper().replace(".", ""))
                        if m else "")
        return ev

    async def poll(self, session) -> None:
        snap = json.loads(self.CONTEXT.read_text())
        age = time.time() - (parse_time(snap.get("generated")) or dt.datetime.fromtimestamp(0, UTC)).timestamp()
        if age > 1800:
            raise RuntimeError("context snapshot is %d minutes old" % (age // 60))
        if "mail_watch" not in snap:
            raise RuntimeError("context snapshot has no mail_watch (Mail closed, or helper not updated)")
        seen = self.state.setdefault("seen", {})
        late = 60 * float(self.cfg.get("announce_within_minutes", 60))
        for msg in snap.get("mail_watch") or []:
            mid = str(msg.get("id") or "")
            if not mid or mid in seen:
                continue
            rule = self.match(msg)
            # unmatched messages: remember only the id, nothing about the message itself
            seen[mid] = ({"at": time.time(), "subject": msg.get("subject", ""), "received": msg.get("received"),
                          "source": rule.get("name")} if rule else {"at": time.time(), "source": None})
            if not rule:
                continue
            ev = self.event_for(rule, msg)
            age = self.age(msg)
            if age is not None and age > late and not ev["kind"].endswith("_urgent"):
                if self.primed:      # old news by now: the briefing lists it (lines), nothing is announced
                    feeds.log.info("mail_watch: %s message received %s seen %d minutes later; briefing only",
                                   rule.get("name", ""), msg.get("received"), age // 60)
                continue
            self.emit(ev)
        cut = time.time() - 10 * 86400
        self.state["seen"] = {k: v for k, v in seen.items() if v["at"] >= cut}

    @staticmethod
    def age(msg: dict, now: Optional[float] = None) -> Optional[float]:
        """Seconds since Mail received the message (date received: local time, no zone); None if unknown."""
        try:
            received = dt.datetime.fromisoformat(str(msg.get("received") or ""))
        except ValueError:
            return None
        return (time.time() if now is None else now) - received.timestamp()

    def lines(self, now):
        out = []
        cut = time.time() - 24 * 3600
        for v in self.state.get("seen", {}).values():
            if v.get("source") and v["at"] >= cut:
                out.append("- %s email: %s" % (v["source"], self.clean_subject(v["subject"])))
        return out[-8:]


feeds.FEEDS.update({"satellite_fires": SatelliteFires, "space_weather": SpaceWeather,
                    "airports": AirportStatus, "launches": RocketLaunches, "mail_watch": MailWatch})
