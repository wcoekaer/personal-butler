import asyncio
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path

import world

UTC = dt.timezone.utc


def feed(cls, cfg=None, primed=True):
    f = cls.__new__(cls)
    f.cfg, f.home, f.interval, f.primed, f.journal = dict(cfg or {}), (37.0, -122.0), 300, primed, None
    f.state, f.events, f.listeners = {}, [], []
    f.listeners.append(f.events.append)
    return f


def run(f, text=None, js=None):
    async def get_text(session, url, **kw):
        return text(url) if callable(text) else text

    async def get_json(session, url, **kw):
        return js
    f.get_text, f.get_json = get_text, get_json
    asyncio.run(f.poll(None))
    return f.events


def rss(items):
    body = "".join("<item><title>%s</title><link>%s</link><guid>%s</guid><description>%s</description>"
                   "<pubDate>%s</pubDate>%s</item>" % (i["title"], i.get("link", i["title"]), i.get("guid", i["title"]),
                                                       i.get("description", ""), i.get("date", "Mon, 16 Nov 2026 05:00:00 GMT"),
                                                       i.get("extra", "")) for i in items)
    return ('<?xml version="1.0"?><rss xmlns:gdacs="http://www.gdacs.org"><channel><title>x</title>%s</channel></rss>' % body)


def gdacs_item(etype, eid, level, country="Philippines", name="", sev="Category 4"):
    extra = ("<gdacs:eventtype>%s</gdacs:eventtype><gdacs:eventid>%s</gdacs:eventid><gdacs:alertlevel>%s</gdacs:alertlevel>"
             "<gdacs:eventname>%s</gdacs:eventname><gdacs:country>%s</gdacs:country><gdacs:severity>%s</gdacs:severity>"
             "<gdacs:iscurrent>true</gdacs:iscurrent>" % (etype, eid, level, name, country, sev))
    return {"title": "%s %s" % (level, etype), "guid": etype + eid + level, "extra": extra}


class GdacsTests(unittest.TestCase):
    def test_red_announced_once_upgrade_counts_types_filtered(self):
        f = feed(world.Gdacs, {"levels": ["red"], "types": ["TC", "FL"]})
        ev = run(f, rss([gdacs_item("TC", "1", "Orange", name="XAVIER-26"), gdacs_item("EQ", "2", "Red")]))
        self.assertEqual(ev, [])                                    # orange not announced, EQ filtered
        self.assertIn("tropical cyclone XAVIER-26", f.lines(None)[0])
        ev = run(f, rss([gdacs_item("TC", "1", "Red", name="XAVIER-26")]))
        self.assertEqual([(e["kind"], e["what"], e["where"]) for e in ev],
                         [("world_disaster", "a tropical cyclone, XAVIER-26", "Philippines")])
        self.assertEqual(run(f, rss([gdacs_item("TC", "1", "Red", name="XAVIER-26")])), ev)


class WorldQuakeTests(unittest.TestCase):
    def test_pager_orange_and_upgrade(self):
        def q(alert, tsunami=0):
            return {"features": [{"id": "us1", "properties": {"mag": 7.6, "place": "near Tokyo, Japan", "alert": alert,
                                                              "tsunami": tsunami, "time": time.time() * 1000}}]}
        f = feed(world.WorldQuakes)
        self.assertEqual(run(f, js=q("yellow")), [])
        ev = run(f, js=q("orange", 1))
        self.assertEqual([(e["level"], e["magnitude"], bool(e["tsunami"])) for e in ev], [("orange", "7.6", True)])
        run(f, js=q("red"))
        self.assertEqual([e["level"] for e in f.events], ["orange", "red"])
        self.assertIn("M7.6 near Tokyo, Japan (USGS impact red)", f.lines(None)[0])


ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Tsunami %s</title>
<entry><id>urn:uuid:%s</id><updated>2026-11-16T05:00:00Z</updated><title>off the coast of Oregon</title>
<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Category: %s Preliminary Magnitude: 8.1(Mwp)
Affected Region: %s Note: * see bulletin</div></summary></entry></feed>"""


class TsunamiTests(unittest.TestCase):
    def test_categories_and_local_urgency(self):
        f = feed(world.Tsunami, {"centers": ["NTWC"], "local_words": ["California"]})
        self.assertEqual(run(f, ATOM % ("Information", "a", "Information Bulletin", "Alaska")), [])
        ev = run(f, ATOM % ("Warning", "b", "Warning", "Coastal areas of California and Oregon"))
        self.assertEqual([(e["kind"], e["category"]) for e in ev], [("tsunami_local", "warning")])
        f.events.clear()
        ev = run(f, ATOM % ("Watch", "c", "Watch", "Hawaii"))
        self.assertEqual([(e["kind"], e["category"], e["where"]) for e in ev], [("tsunami_alert", "watch", "Hawaii")])


class BreakingNewsTests(unittest.TestCase):
    def test_needs_score_and_two_outlets(self):
        f = feed(world.BreakingNews, {"threshold": 9, "corroborate": 2,
                                      "sources": [{"name": "A", "url": "a"}, {"name": "B", "url": "b"}]})
        replies = []

        def ask(prompt):
            items = []
            for line in prompt.split("New headlines")[1].splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3 and parts[0].isdigit():
                    big = "volcano" in parts[2].lower()
                    items.append({"id": int(parts[0]), "score": 10 if big else 3,
                                  "story": "supervolcano" if big else "other-%s" % parts[0],
                                  "summary": "A supervolcano has erupted" if big else parts[2]})
            replies.append(len(items))
            return "```json\n" + json.dumps({"items": items}) + "\n```"
        f.ask = ask
        feeds_now = {"a": [{"title": "Old story"}], "b": [{"title": "Older story"}]}
        f.primed = False
        run(f, text=lambda url: rss(feeds_now[url]))
        self.assertEqual(replies, [])                               # first poll: record only, no LLM call
        f.primed = True
        feeds_now["a"].append({"title": "Supervolcano erupts"})
        feeds_now["a"].append({"title": "Local bake sale"})
        run(f, text=lambda url: rss(feeds_now[url]))
        self.assertEqual(f.events, [])                              # one outlet only
        feeds_now["b"].append({"title": "Massive volcano eruption reported"})
        ev = run(f, text=lambda url: rss(feeds_now[url]))
        self.assertEqual([(e["kind"], e["summary"], e["sources"]) for e in ev],
                         [("breaking_news", "A supervolcano has erupted.", "A and B")])
        run(f, text=lambda url: rss(feeds_now[url]))
        self.assertEqual(len(f.events), 1)                          # announced once
        self.assertIn("Major news: A supervolcano has erupted", f.lines(None)[0])


class TravelTests(unittest.TestCase):
    def test_level_rise_for_watched_and_calendar_countries(self):
        tmp = tempfile.TemporaryDirectory()
        world.TravelAdvisories.CONTEXT = Path(tmp.name) / "context.json"
        world.TravelAdvisories.CONTEXT.write_text(json.dumps({"events": [{"title": "Flight to Japan"}]}))
        f = feed(world.TravelAdvisories, {"countries": ["France"]})
        page = lambda j, fr: rss([{"title": "Japan - Level %d: Exercise Increased Caution" % j},
                                  {"title": "France - Level %d: Reconsider Travel" % fr},
                                  {"title": "Peru - Level 3: Reconsider Travel"}])
        self.assertEqual(run(f, page(1, 2)), [])
        self.assertEqual(f.state["trip"], ["Japan"])
        ev = run(f, page(2, 3))
        self.assertEqual(sorted((e["country"], e["level"]) for e in ev), [("France", "3"), ("Japan", "2")])
        self.assertIn("Japan (on your calendar): Level 2", f.lines(None)[0])
        tmp.cleanup()


class CloudTests(unittest.TestCase):
    def test_major_filters(self):
        now = time.time()
        recent = dt.datetime.fromtimestamp(now - 600, UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        f = feed(world.CloudStatus)
        aws = rss([{"title": "Service disruption: Increased error rates in US-EAST-1", "date": recent},
                   {"title": "Service impact: Elevated latency", "date": recent},
                   {"title": "Service is operating normally: [RESOLVED] Increased error rates", "date": recent}])
        self.assertEqual([x["title"] for x in f.major("AWS", aws, now)], ["Increased error rates in US-EAST-1"])
        gcp = json.dumps([{"id": "1", "severity": "high", "end": None, "begin": dt.datetime.fromtimestamp(now - 900, UTC).isoformat(),
                           "external_desc": "Multiple services down"}, {"id": "2", "severity": "medium", "end": None,
                           "begin": dt.datetime.fromtimestamp(now - 900, UTC).isoformat(), "external_desc": "minor"}])
        self.assertEqual([x["id"] for x in f.major("Google Cloud", gcp, now)], ["1"])
        cf = json.dumps({"incidents": [{"id": "c1", "impact": "minor", "name": "x"}, {"id": "c2", "impact": "critical", "name": "Global outage"}]})
        self.assertEqual([x["id"] for x in f.major("Cloudflare", cf, now)], ["c2"])
        oci = rss([{"title": "Networking | Multiple Regions | CN-1", "date": recent}, {"title": "Compute | US East (Ashburn) | x", "date": recent}])
        self.assertEqual(len(f.major("Oracle Cloud", oci, now)), 1)


if __name__ == "__main__":
    unittest.main()
