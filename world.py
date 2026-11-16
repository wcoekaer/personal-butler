"""World and national feeds (registered into feeds.FEEDS; opt-in like outside.py:
each runs only when its section exists under `feeds:` in config.yaml).

  gdacs              UN/EC GDACS global disasters at Red (or chosen) alert level
  world_quakes       USGS significant earthquakes worldwide with PAGER orange/red impact
  tsunami            NOAA tsunami warning centers: warnings, advisories, watches
  breaking_news      a few news RSS feeds, triaged by the LLM; only top-tier stories that
                     at least two outlets carry are announced
  travel_advisories  US State Department levels for watched countries and countries on the calendar
  cloud_status       major incidents at AWS, Azure, Google Cloud, Cloudflare and OCI
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional

import feeds
from feeds import Feed, USER_AGENT, parse_time

UTC = dt.timezone.utc
NS = {"gdacs": "http://www.gdacs.org", "atom": "http://www.w3.org/2005/Atom",
      "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}


def strip_html(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def rss_items(xml_text: str) -> List[dict]:
    """title/link/guid/description/pubDate (epoch) of RSS 2.0 <item>s."""
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        when = it.findtext("pubDate") or ""
        try:
            ts = email.utils.parsedate_to_datetime(when).timestamp() if when else 0.0
        except (TypeError, ValueError):
            ts = 0.0
        out.append({"title": strip_html(it.findtext("title") or ""), "link": (it.findtext("link") or "").strip(),
                    "guid": (it.findtext("guid") or it.findtext("link") or it.findtext("title") or "").strip(),
                    "description": strip_html(it.findtext("description") or ""), "ts": ts, "el": it})
    return out


def join_names(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class WorldFeed(Feed):
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


# --------------------------------------------------------------------------- GDACS


def places(text: str, limit: int = 3) -> str:
    """'Aruba, , Brazil, Colombia, Costa Rica' -> 'Aruba, Brazil and Colombia and 1 more'."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        return "an unspecified region"
    if len(parts) <= limit:
        return join_names(parts)
    return "%s and %d more" % (", ".join(parts[:limit]), len(parts) - limit)


class Gdacs(WorldFeed):
    name = "gdacs"
    URL = "https://www.gdacs.org/xml/rss.xml"
    ORDER = {"green": 0, "orange": 1, "red": 2}
    WHAT = {"EQ": "an earthquake", "TC": "a tropical cyclone", "FL": "flooding", "VO": "a volcanic eruption",
            "DR": "a drought", "WF": "a wildfire", "TS": "a tsunami"}

    def parse(self, xml_text: str) -> List[dict]:
        out = []
        for it in rss_items(xml_text):
            el = it["el"]
            g = lambda tag: (el.findtext("gdacs:" + tag, namespaces=NS) or "").strip()
            out.append({"type": g("eventtype"), "id": g("eventid"), "level": g("alertlevel").lower(),
                        "name": g("eventname"), "country": g("country"), "severity": g("severity"),
                        "population": g("population"), "current": g("iscurrent").lower() == "true",
                        "title": it["title"], "ts": it["ts"]})
        return out

    async def poll(self, session) -> None:
        events = self.parse(await self.get_text(session, self.URL))
        levels = {l.lower() for l in self.cfg.get("levels", ["red"])}
        types = set(self.cfg.get("types", list(self.WHAT)))
        said = self.state.setdefault("announced", {})
        current = []
        for e in events:
            if e["type"] not in types or not e["current"]:
                continue
            current.append({k: e[k] for k in ("type", "id", "level", "name", "country", "severity")})
            key = e["type"] + e["id"]
            if e["level"] in levels and self.ORDER.get(e["level"], 0) > self.ORDER.get(said.get(key, ""), -1):
                said[key] = e["level"]
                what = self.WHAT.get(e["type"], "a disaster")
                self.emit({"kind": "world_disaster", "device": key, "level": e["level"],
                           "what": what + (", " + e["name"] if e["name"] else ""),
                           "where": places(e["country"]),
                           "severity": e["severity"], "population": e["population"]})
        self.state["current"] = current

    def _important(self):
        return [e for e in self.state.get("current", []) if e["level"] in ("orange", "red")]

    def lines(self, now):
        return ["- GDACS %s alert: %s%s in %s (%s)" % (e["level"], self.WHAT.get(e["type"], "a disaster").split(" ", 1)[-1],
                (" " + e["name"]) if e["name"] else "", places(e["country"]), e["severity"]) for e in self._important()]

    def hud_items(self, now):
        return [{"label": "GDACS %s · %s" % (e["level"], self.WHAT.get(e["type"], "disaster").split(" ", 1)[-1]),
                 "value": places(e["country"], 2)[:28], "level": "warn" if e["level"] == "red" else ""}
                for e in self._important()[:3]]


# --------------------------------------------------------------------------- USGS significant quakes


class WorldQuakes(WorldFeed):
    name = "world_quakes"
    URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson"
    ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
    IMPACT = {"orange": "significant casualties and damage are likely",
              "red": "extensive casualties and damage are likely"}

    async def poll(self, session) -> None:
        d = await self.get_json(session, self.URL)
        levels = {l.lower() for l in self.cfg.get("levels", ["orange", "red"])}
        said = self.state.setdefault("announced", {})
        recent = []
        for f in d.get("features", []):
            p = f.get("properties") or {}
            alert = (p.get("alert") or "").lower()
            q = {"id": f.get("id"), "mag": p.get("mag"), "place": p.get("place") or "", "alert": alert,
                 "tsunami": bool(p.get("tsunami")), "ts": (p.get("time") or 0) / 1000.0}
            recent.append(q)
            if alert in levels and self.ORDER.get(alert, 0) > self.ORDER.get(said.get(q["id"], ""), -1):
                said[q["id"]] = alert
                self.emit({"kind": "world_quake", "device": q["id"], "magnitude": "%.1f" % (q["mag"] or 0),
                           "place": q["place"], "level": alert, "impact": self.IMPACT.get(alert, ""),
                           "tsunami": " A tsunami may have been generated." if q["tsunami"] else ""})
        self.state["recent"] = recent

    def lines(self, now):
        cut = time.time() - 86400
        return ["- Significant earthquake: M%.1f %s%s" % (q["mag"] or 0, q["place"],
                (" (USGS impact %s)" % q["alert"]) if q["alert"] in ("orange", "red") else "")
                for q in self.state.get("recent", []) if q["ts"] >= cut and (q["mag"] or 0) >= 6]

    def hud_items(self, now):
        cut = time.time() - 86400
        return [{"label": "M%.1f quake" % (q["mag"] or 0), "value": q["place"][:30], "level": "warn"}
                for q in self.state.get("recent", []) if q["ts"] >= cut and q["alert"] in ("orange", "red")][:2]


# --------------------------------------------------------------------------- NOAA tsunami


class Tsunami(WorldFeed):
    name = "tsunami"
    URLS = {"NTWC": "https://www.tsunami.gov/events/xml/PAAQAtom.xml",     # US/Canada coasts, Alaska
            "PTWC": "https://www.tsunami.gov/events/xml/PHEBAtom.xml"}     # Hawaii, Pacific
    CATEGORY = re.compile(r"Category:\s*(Warning|Advisory|Watch|Information|Threat)", re.I)

    def parse(self, xml_text: str, center: str) -> List[dict]:
        out = []
        root = ET.fromstring(xml_text)
        for e in root.findall("atom:entry", NS):
            summary = strip_html(ET.tostring(e.find("atom:summary", NS), encoding="unicode")
                                 if e.find("atom:summary", NS) is not None else "")
            m = self.CATEGORY.search(summary)
            region = re.search(r"Affected Region:\s*(.+?)(?:\s+Note:|\s+Definition:|$)", summary)
            out.append({"id": "%s|%s|%s" % (center, e.findtext("atom:id", default="", namespaces=NS),
                                            e.findtext("atom:updated", default="", namespaces=NS)),
                        "where": e.findtext("atom:title", default="", namespaces=NS).strip(),
                        "category": (m.group(1).title() if m else "Information"),
                        "region": region.group(1).strip() if region else "", "summary": summary[:400]})
        return out

    async def poll(self, session) -> None:
        local = [w.lower() for w in self.cfg.get("local_words", ["California"])]
        said = self.state.setdefault("announced", {})
        active = []
        for center in self.cfg.get("centers", list(self.URLS)):
            for b in self.parse(await self.get_text(session, self.URLS[center]), center):
                if b["category"] not in ("Warning", "Advisory", "Watch"):
                    continue
                active.append(b)
                if b["id"] in said:
                    continue
                said[b["id"]] = time.time()
                text = (b["region"] + " " + b["summary"]).lower()
                is_local = any(w in text for w in local)
                self.emit({"kind": "tsunami_local" if is_local and b["category"] in ("Warning", "Advisory") else "tsunami_alert",
                           "device": b["id"][:80], "category": b["category"].lower(),
                           "where": b["region"] or b["where"], "center": center})
        self.state["active"] = active[:10]
        self.state["announced"] = {k: v for k, v in said.items() if time.time() - v < 14 * 86400}

    def lines(self, now):
        return ["- Tsunami %s (%s): %s" % (b["category"].lower(), "NOAA", b["region"] or b["where"])
                for b in self.state.get("active", [])]

    def hud_items(self, now):
        return [{"label": "Tsunami " + b["category"].lower(), "value": (b["region"] or b["where"])[:30], "level": "warn"}
                for b in self.state.get("active", [])[:2]]


# --------------------------------------------------------------------------- breaking news


TRIAGE_PROMPT = """You triage news headlines for a household alert system. It must stay silent
except for historic, top-tier national or international events.

Score each new headline from 0 to 10:
10 = historic: war breaking out between major powers; the death or assassination of a sitting
     head of state of a major country; a catastrophic terrorist attack; a nuclear or radiological
     incident; a natural disaster with thousands dead; a nationwide infrastructure collapse.
9  = a major breaking emergency of national or international importance: a large terrorist
     attack; a major escalation of a war; a US president incapacitated; a stock market crash of
     more than 7 percent in a day; a major airliner crash with many deaths.
8 or less = everything else, including politics, elections, ongoing conflicts without a major
     new development, court cases, business and markets on normal days, opinion, analysis,
     sports, celebrities, science, and anything already old.

Group headlines about the same event under the same short kebab-case story key. Reuse the key
of an existing story when a headline is about it.

Existing stories (key: summary):
{stories}

New headlines (id | outlet | headline | description):
{headlines}

Reply with JSON only, no prose:
{{"items": [{{"id": <id>, "score": <0-10>, "story": "<key>", "summary": "<one short, neutral spoken sentence without outlet names>"}}]}}"""


class BreakingNews(WorldFeed):
    """Major breaking news: new headlines from a few outlets are scored by the LLM; a story is
    announced once when it scores at least `threshold` and at least `corroborate` different
    outlets carry it within `window_minutes`. `ask` (prompt -> text) is set by jarvis.py."""

    name = "breaking_news"
    ask: Optional[Callable[[str], str]] = None
    SOURCES = [{"name": "BBC", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
               {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml"},
               {"name": "The New York Times", "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"}]

    def triage(self, items: List[dict]) -> List[dict]:
        if not items or not self.ask:
            return []
        stories = self.state.get("stories", {})
        known = "\n".join("%s: %s" % (k, s["summary"]) for k, s in stories.items()) or "(none)"
        heads = "\n".join("%d | %s | %s | %s" % (i, it["source"], it["title"], it["description"][:160])
                          for i, it in enumerate(items))
        text = self.ask(TRIAGE_PROMPT.format(stories=known, headlines=heads))
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            return (json.loads(m.group(0)) if m else {}).get("items", [])
        except ValueError:
            return []

    def apply(self, items: List[dict], scored: List[dict], now: float) -> None:
        stories = self.state.setdefault("stories", {})
        threshold = int(self.cfg.get("threshold", 9))
        need = int(self.cfg.get("corroborate", 2))
        window = 60 * float(self.cfg.get("window_minutes", 90))
        for s in scored:
            try:
                it = items[int(s["id"])]
                score = int(s.get("score", 0))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            key = re.sub(r"[^a-z0-9-]", "", str(s.get("story") or "").lower())[:60] or "story-%d" % int(now)
            st = stories.setdefault(key, {"score": 0, "sources": {}, "summary": "", "first": now, "announced": False})
            if score >= st["score"]:
                st["score"], st["summary"] = score, (s.get("summary") or it["title"]).strip()
            st["sources"][it["source"]] = now
            outlets = [o for o, t in st["sources"].items() if now - t <= window]
            if st["score"] >= threshold and len(outlets) >= need and not st["announced"]:
                st["announced"] = True
                self.emit({"kind": "breaking_news", "device": key, "summary": st["summary"].rstrip(".") + ".",
                           "sources": join_names(sorted(outlets)), "score": str(st["score"])})
        self.state["stories"] = {k: v for k, v in stories.items() if now - max(v["sources"].values() or [now]) < 2 * 86400}

    async def poll(self, session) -> None:
        seen = self.state.setdefault("seen", {})
        fresh = []
        for src in self.cfg.get("sources", self.SOURCES):
            try:
                for it in rss_items(await self.get_text(session, src["url"])):
                    key = it["guid"] or it["link"]
                    if key and key not in seen:
                        seen[key] = time.time()
                        fresh.append({"source": src["name"], "title": it["title"], "description": it["description"]})
            except Exception as e:
                feeds.log.warning("breaking news source %s failed: %s", src.get("name"), str(e)[:80])
        now = time.time()
        self.state["seen"] = {k: v for k, v in seen.items() if now - v < 3 * 86400}
        if not self.primed:
            return                               # first run: record what is already out, triage nothing
        cap = int(self.cfg.get("max_per_poll", 40))
        for i in range(0, min(len(fresh), 4 * cap), cap):
            batch = fresh[i:i + cap]
            scored = await asyncio.to_thread(self.triage, batch)     # the LLM call blocks; keep the loop free
            self.apply(batch, scored, now)

    def lines(self, now):
        cut = time.time() - 12 * 3600
        return ["- Major news: %s (%s)" % (s["summary"], join_names(sorted(s["sources"])))
                for s in self.state.get("stories", {}).values()
                if s["score"] >= int(self.cfg.get("threshold", 9)) - 1 and max(s["sources"].values()) >= cut]

    def hud_items(self, now):
        cut = time.time() - 3 * 3600
        top = [s for s in self.state.get("stories", {}).values() if s.get("announced") and max(s["sources"].values()) >= cut]
        return [{"label": "Breaking", "value": s["summary"][:44], "level": "warn"} for s in top[:1]]


# --------------------------------------------------------------------------- travel advisories


class TravelAdvisories(WorldFeed):
    """State Department advisory levels for `countries` plus any country named in a calendar
    event (today/tomorrow): a level going up is announced; trip countries are in briefings."""

    name = "travel_advisories"
    URL = "https://travel.state.gov/_res/rss/TAsTWs.xml"
    CONTEXT = Path(os.path.expanduser("~/Library/Caches/jarvis/context.json"))
    TITLE = re.compile(r"^(.*?)\s+-\s+Level\s+(\d)\s*:\s*(.+)$")

    def parse(self, xml_text: str) -> Dict[str, dict]:
        out = {}
        for it in rss_items(xml_text):
            m = self.TITLE.match(it["title"])
            if m:
                out[m.group(1).strip()] = {"level": int(m.group(2)), "text": m.group(3).strip(), "ts": it["ts"]}
        return out

    def trip_countries(self, names) -> List[str]:
        try:
            events = json.loads(self.CONTEXT.read_text()).get("events") or []
        except (OSError, ValueError):
            return []
        titles = " ".join(str(e.get("title", "")) for e in events).lower()
        return sorted(n for n in names if re.search(r"\b%s\b" % re.escape(n.lower()), titles))

    async def poll(self, session) -> None:
        levels = self.parse(await self.get_text(session, self.URL))
        watched = set(self.cfg.get("countries", [])) | set(self.trip_countries(levels))
        prev = self.state.get("levels", {})
        min_level = int(self.cfg.get("alert_level", 2))
        for country in sorted(watched):
            cur = levels.get(country)
            if not cur:
                continue
            old = prev.get(country, {}).get("level")
            if old is not None and cur["level"] > old and cur["level"] >= min_level:
                self.emit({"kind": "travel_advisory", "device": country, "country": country,
                           "level": str(cur["level"]), "text": cur["text"].lower().rstrip(".")})
        self.state["levels"] = levels
        self.state["trip"] = self.trip_countries(levels)

    def lines(self, now):
        lv = self.state.get("levels", {})
        out = ["- Travel advisory for %s (on your calendar): Level %d, %s" % (c, lv[c]["level"], lv[c]["text"])
               for c in self.state.get("trip", []) if c in lv]
        out += ["- Travel advisory for %s: Level %d, %s" % (c, lv[c]["level"], lv[c]["text"])
                for c in self.cfg.get("countries", []) if c in lv and c not in self.state.get("trip", []) and lv[c]["level"] >= 3]
        return out


# --------------------------------------------------------------------------- cloud status


class CloudStatus(WorldFeed):
    """Major incidents only: AWS "Service disruption", Google Cloud high severity, Cloudflare
    major/critical, any Azure status-page incident (it lists only broad ones), OCI incidents
    across multiple regions."""

    name = "cloud_status"
    URLS = {"AWS": "https://status.aws.amazon.com/rss/all.rss",
            "Azure": "https://azure.status.microsoft/en-us/status/feed/",
            "Google Cloud": "https://status.cloud.google.com/incidents.json",
            "Cloudflare": "https://www.cloudflarestatus.com/api/v2/incidents/unresolved.json",
            "Oracle Cloud": "https://ocistatus.oraclecloud.com/history.rss"}

    def major(self, provider: str, text: str, now: float) -> List[dict]:
        recent = 3 * 3600
        out = []
        if provider == "Google Cloud":
            for x in json.loads(text):
                begin = parse_time(x.get("begin"))
                if x.get("severity") == "high" and not x.get("end") and begin and now - begin.timestamp() < 86400:
                    out.append({"id": x.get("id"), "title": strip_html(x.get("external_desc", ""))[:120]})
        elif provider == "Cloudflare":
            for x in json.loads(text).get("incidents", []):
                if x.get("impact") in ("major", "critical"):
                    out.append({"id": x.get("id"), "title": x.get("name", "")[:120]})
        else:
            for it in rss_items(text):
                if it["ts"] and now - it["ts"] > recent:
                    continue
                t = it["title"]
                if "[RESOLVED]" in t.upper() or "operating normally" in t.lower():
                    continue
                if ((provider == "AWS" and t.lower().startswith("service disruption"))
                        or provider == "Azure"
                        or (provider == "Oracle Cloud" and "multiple regions" in t.lower())):
                    out.append({"id": it["guid"] or t, "title": re.sub(r"^service disruption:\s*", "", t, flags=re.I)[:120]})
        return out

    async def poll(self, session) -> None:
        now = time.time()
        said = self.state.setdefault("announced", {})
        active = []
        for provider in self.cfg.get("providers", list(self.URLS)):
            try:
                found = self.major(provider, await self.get_text(session, self.URLS[provider]), now)
            except Exception as e:
                feeds.log.warning("cloud status %s failed: %s", provider, str(e)[:80])
                continue
            for x in found:
                active.append(dict(x, provider=provider))
                key = "%s|%s" % (provider, x["id"])
                if key not in said:
                    said[key] = now
                    self.emit({"kind": "cloud_outage", "device": key[:80], "provider": provider,
                               "title": x["title"].rstrip(".")})
        self.state["active"] = active
        self.state["announced"] = {k: v for k, v in said.items() if now - v < 7 * 86400}

    def lines(self, now):
        return ["- Major cloud incident at %s: %s" % (x["provider"], x["title"]) for x in self.state.get("active", [])]

    def hud_items(self, now):
        return [{"label": x["provider"] + " incident", "value": x["title"][:36], "level": "warn"}
                for x in self.state.get("active", [])[:2]]


feeds.FEEDS.update({"gdacs": Gdacs, "world_quakes": WorldQuakes, "tsunami": Tsunami,
                    "breaking_news": BreakingNews, "travel_advisories": TravelAdvisories,
                    "cloud_status": CloudStatus})
