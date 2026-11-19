import asyncio
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path

import jarvis
import outside

HOME = (37.0, -122.0)
UTC = dt.timezone.utc


def feed(cls, cfg=None):
    f = cls.__new__(cls)
    f.cfg, f.home, f.interval, f.primed, f.journal = dict(cfg or {}), HOME, 300, True, None
    f.state, f.events, f.listeners = {}, [], []
    f.listeners.append(f.events.append)
    return f


def run(f, **canned):
    async def text(session, url, **kw):
        return canned["text"](url) if callable(canned.get("text")) else canned["text"]

    async def js(session, url, **kw):
        return canned["json"]
    f.get_text, f.get_json = text, js
    asyncio.run(f.poll(None))
    return f.events


class SatelliteFiresTests(unittest.TestCase):
    def csv(self, rows):
        head = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
        return head + "".join("%s,%s,330,0.4,0.4,%s,%s,N20,VIIRS,%s,2.0NRT,290,5.1,N\n" % r for r in rows)

    def test_one_announcement_per_area_and_low_confidence_dropped(self):
        os_env = {"FIRMS_MAP_KEY": "x"}
        import os
        os.environ.update(os_env)
        today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
        rows = [(37.03, -122.0, today, "0412", "n"), (37.031, -122.001, today, "0412", "h"),
                (37.05, -122.05, today, "0412", "l"), (37.20, -122.0, today, "0412", "n")]
        f = feed(outside.SatelliteFires, {"sources": ["VIIRS_NOAA20_NRT"], "alert_miles": 10, "watch_miles": 25})
        ev = run(f, text=self.csv(rows))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["direction"], ev[0]["count"]), ("satellite_fire", "north", "2"))
        self.assertEqual(ev[0]["satellite"], "NOAA-20 VIIRS")
        self.assertEqual(len(f.recent()), 3)                      # 3 kept (low confidence dropped), 1 beyond alert range
        self.assertEqual(run(f, text=self.csv(rows)), ev)          # same detections: nothing new


class SpaceWeatherTests(unittest.TestCase):
    def scales(self, now, forecast=(0, 0, 0)):
        d = {"0": {"DateStamp": "2026-11-16", "G": {"Scale": str(now)}}}
        for i, g in enumerate(forecast, 1):
            d[str(i)] = {"DateStamp": "2026-11-%d" % (16 + i), "G": {"Scale": str(g)}}
        return d

    def test_storm_announced_once_per_level_and_watch(self):
        f = feed(outside.SpaceWeather)
        self.assertEqual(run(f, json=self.scales(2)), [])
        ev = run(f, json=self.scales(3))
        self.assertEqual([(e["kind"], e["level"], e["aurora"]) for e in ev], [("space_weather_storm", "G3", "")])
        self.assertEqual(run(f, json=self.scales(3)), ev)          # no repeat
        ev = run(f, json=self.scales(4))
        self.assertIn("Auroras", ev[-1]["aurora"])
        f.events.clear()
        ev = run(f, json=self.scales(0, forecast=(0, 3, 1)))
        self.assertEqual([(e["kind"], e["level"], e["day"]) for e in ev], [("space_weather_watch", "G3", "Wednesday")])
        self.assertIn("G3 (strong) storm expected Wednesday", "\n".join(f.lines(dt.datetime.now())))


FAA = """<AIRPORT_STATUS_INFORMATION><Update_Time>now</Update_Time>
<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>SFO</ARPT>
<Reason>low ceilings</Reason><Avg>2 hours and 46 minutes</Avg><Max>4 hours</Max></Ground_Delay></Ground_Delay_List></Delay_type>
<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List><Program><ARPT>OAK</ARPT><Reason>thunderstorms</Reason>
<End_Time>8:15 pm PST</End_Time></Program></Ground_Stop_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List><Airport><ARPT>SJC</ARPT><Reason>!SJC AD AP CLSD</Reason>
<Start>Nov 15 at 07:30 UTC.</Start><Reopen>Nov 21 at 14:15 UTC.</Reopen></Airport>
<Airport><ARPT>SNA</ARPT><Reason>!SNA AD AP CLSD</Reason><Reopen>Nov 21 at 14:15 UTC.</Reopen></Airport></Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""


class AirportTests(unittest.TestCase):
    def test_ground_stops_and_closures_announced_delays_in_briefing(self):
        f = feed(outside.AirportStatus, {"airports": ["SFO", "OAK", "SJC"]})
        ev = run(f, text=FAA)
        self.assertEqual(sorted((e["kind"], e["code"]) for e in ev),
                         [("airport_closure", "SJC"), ("airport_ground_stop", "OAK")])
        stop = next(e for e in ev if e["code"] == "OAK")
        self.assertEqual((stop["airport"], stop["reason_text"], stop["until"]),
                         ("Oakland", " because of thunderstorms", "until 8:15 pm pst"))
        self.assertTrue(next(e for e in ev if e["code"] == "SJC")["until"].startswith("until "))
        self.assertEqual(run(f, text=FAA), ev)                      # still active: no repeat
        lines = "\n".join(f.lines(dt.datetime.now()))
        self.assertIn("SFO airport: ground delay program (low ceilings), average 2 hours and 46 minutes", lines)


class LaunchTests(unittest.TestCase):
    def launch(self, net):
        return {"results": [{"id": "abc", "name": "Falcon 9 Block 5 | NROL-1", "net": net.isoformat().replace("+00:00", "Z"),
                             "status": {"abbrev": "Go"}, "net_precision": {"abbrev": "MIN"},
                             "pad": {"latitude": 34.632, "longitude": -120.611,
                                     "location": {"name": "Vandenberg SFB, CA, USA"}}}]}

    def test_visibility_and_heads_up(self):
        f = feed(outside.RocketLaunches, {"lead_minutes": 30})
        night = dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC)        # 11 PM PST
        day = dt.datetime(2026, 11, 19, 20, 0, tzinfo=UTC)         # noon PST
        dusk = dt.datetime(2026, 11, 19, 1, 25, tzinfo=UTC)        # ~30 min after sunset
        self.assertEqual((f.visibility(night), f.visibility(day), f.visibility(dusk)), ("night", None, "twilight"))
        soon = dt.datetime.now(UTC) + dt.timedelta(minutes=31)
        f.visibility = lambda when: "twilight"
        ev = run(f, json=self.launch(soon))
        self.assertEqual([(e["kind"], e["rocket"], e["site"], e["direction"]) for e in ev],
                         [("rocket_launch", "Falcon 9", "Vandenberg SFB", "south-southeast")])
        self.assertIn("glowing plume", ev[0]["show"])
        self.assertEqual(f.interval, 600.0)                         # polls faster near a launch
        self.assertEqual(run(f, json=self.launch(soon)), ev)       # once per launch


class MailWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        outside.MailWatch.CONTEXT = Path(self.tmp.name) / "context.json"
        outside.MailWatch.WATCH = Path(self.tmp.name) / "mail-watch.json"
        cfg = {"rules": [
            {"name": "County", "sender": "alerts@county.example", "kind": "county_alert", "urgent_words": ["evacuat"]},
            {"name": "UPS", "sender": "ups.com", "subject": "Scheduled for Delivery Today", "kind": "package_today", "dedupe": True}]}
        self.f = outside.MailWatch(cfg, HOME)
        self.f.primed, self.f.events, self.f.listeners = True, [], []
        self.f.listeners.append(self.f.events.append)

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self, msgs):
        outside.MailWatch.CONTEXT.write_text(json.dumps({"generated": dt.datetime.now(UTC).isoformat(), "mail_watch": msgs}))
        asyncio.run(self.f.poll(None))
        return self.f.events

    def test_rules_urgency_window_and_watch_file(self):
        self.assertEqual(json.loads(outside.MailWatch.WATCH.read_text())["senders"], ["alerts@county.example", "ups.com"])
        ev = self.snap([
            {"id": "1", "sender": "County <alerts@county.example>", "subject": "Road closure on Main St", "excerpt": "Expect delays."},
            {"id": "2", "sender": "alerts@county.example", "subject": "Evacuation order: zone 12", "excerpt": "Leave now."},
            {"id": "3", "sender": "UPS <mcinfo@ups.com>", "subject": "UPS Update: Package Scheduled for Delivery Today",
             "excerpt": "Estimated delivery window: 10:30 AM - 2:30 PM"},
            {"id": "4", "sender": "UPS <mcinfo@ups.com>", "subject": "Save 20% on shipping", "excerpt": "promo"}])
        self.assertEqual([e["kind"] for e in ev], ["county_alert", "county_alert_urgent", "package_today"])
        self.assertEqual(ev[2]["window"], ", between 10:30 AM and 2:30 PM")
        self.assertEqual(ev[2]["device"], "UPS")
        self.assertEqual(self.snap([{"id": "1", "sender": "alerts@county.example", "subject": "Road closure"}]), ev)

    def test_a_late_message_goes_to_the_briefing_only(self):
        """Mail stalled for hours on Nov 18: six alerts were announced together, late. A message
        first seen more than `announce_within_minutes` after Mail received it is listed in
        briefings only, unless it is urgent."""
        now = dt.datetime.now().replace(microsecond=0)

        def received(minutes_ago):          # Mail's date received: local time, no zone
            return (now - dt.timedelta(minutes=minutes_ago)).isoformat()

        ev = self.snap([
            {"id": "1", "sender": "alerts@county.example", "subject": "Road closure on Main St", "received": received(5)},
            {"id": "2", "sender": "alerts@county.example", "subject": "Road closure on Elm St", "received": received(240)},
            {"id": "3", "sender": "alerts@county.example", "subject": "Evacuation order: zone 12", "received": received(240)},
            {"id": "4", "sender": "alerts@county.example", "subject": "Road closure on Oak St"}])      # no time: announced
        self.assertEqual([e["subject"] for e in ev],
                         ["Road closure on Main St", "Evacuation order: zone 12", "Road closure on Oak St"])
        self.assertIn("- County email: Road closure on Elm St", self.f.lines(now))
        self.f.cfg["announce_within_minutes"] = 300
        self.snap([{"id": "5", "sender": "alerts@county.example", "subject": "Road closure on Pine St",
                    "received": received(240)}])
        self.assertEqual(ev[-1]["subject"], "Road closure on Pine St")


class MailTextTests(unittest.TestCase):
    def test_subject_and_summary_cleanup(self):
        clean, summ = outside.MailWatch.clean_subject, outside.MailWatch.summarize
        self.assertEqual(clean("Alert Message: AVOID MAIN AND 1ST INTERSECTION DUE TO A COLLISION."),
                         "Avoid main and 1st intersection due to a collision")
        self.assertEqual(clean("2026_Road Closure - Update"), "Road Closure - Update")
        self.assertEqual(clean("County Alert: Road Closure", r"^(?:county alert:\s*)"), "Road Closure")
        body = ("County Alert: County Sheriff's Office ROAD CLOSURE - Elm Road between Oak Lane and Pine Way is closed "
                "for 2 hours. Updates will follow. \ufffc Click the following link to unsubscribe.")
        self.assertEqual(summ(body), "Elm Road between Oak Lane and Pine Way is closed for 2 hours. Updates will follow.")
        long = "Word " * 80 + "end."
        self.assertTrue(summ(long).endswith("…") and len(summ(long)) <= 241)

    ALERTSF = ("AlertSF is owned and operated by the City and County of San Francisco and powered by Everbridge, Inc.\n"
               "ALERTSF: Flood Advisory in effect until 11/20. Heavy rain and strong winds may cause urban flooding, "
               "debris, mudslides in steep areas, road closures, fallen/downed trees, and power outages. Avoid "
               "unnecessary travel, secure outdoor items, and report flooding to 311.\n\nFor more information on "
               "current emergencies in San Francisco visit sf.gov/readysf\n\nClick here for more information on this "
               "alert.\nTo stop receiving future email notifications from this organization unsubscribe here.")

    def test_everbridge_boilerplate_and_subject_repeat(self):
        summ = outside.MailWatch.summarize
        self.assertEqual(summ(self.ALERTSF), "Flood Advisory in effect until 11/20. Heavy rain and strong winds may "
                         "cause urban flooding, debris, mudslides in steep areas, road closures, fallen/downed trees, "
                         "and power outages.")
        # the subject is the first line of the body: skip it rather than say it twice
        self.assertEqual(summ(self.ALERTSF, skip="Flood advisory in effect until 11/20"),
                         "Heavy rain and strong winds may cause urban flooding, debris, mudslides in steep areas, road "
                         "closures, fallen/downed trees, and power outages. Avoid unnecessary travel, secure outdoor "
                         "items, and report flooding to 311.")

    def test_spoken_dates(self):
        say = outside.MailWatch.spoken_dates
        year = dt.date.today().year
        self.assertEqual(say("Flood Advisory in effect until 11/20."), "Flood Advisory in effect until November 20.")
        self.assertEqual(say("Closed from 1/5/%d through 01/09" % year), "Closed from January 5 through January 9")
        self.assertEqual(say("Open again on 3/2/%d" % (year + 1)), "Open again on March 2, %d" % (year + 1))
        self.assertEqual(say("Up to 1/2 inch of rain; by 1/2 inch"), "Up to 1/2 inch of rain; by 1/2 inch")
        self.assertEqual(say("until 2/30 or 13/4"), "until 2/30 or 13/4")

    def test_alertsf_event(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outside.MailWatch.CONTEXT = Path(tmp.name) / "context.json"
        outside.MailWatch.WATCH = Path(tmp.name) / "mail-watch.json"
        f = outside.MailWatch({"rules": [{"name": "San Francisco", "sender": "noreply@sf.gov", "kind": "city_alert",
                                          "subject_strip": r"^(?:alert ?sf\s*[:-]\s*)",
                                          "urgent_words": ["evacuat"]}]}, HOME)
        ev = f.event_for(f.rules[0], {"id": "x", "sender": "AlertSF <noreply@sf.gov>",
                                      "subject": "ALERTSF: Flood Advisory in effect until 11/20", "excerpt": self.ALERTSF})
        self.assertEqual(ev["kind"], "city_alert")
        self.assertEqual(ev["subject"], "Flood Advisory in effect until November 20")
        self.assertTrue(ev["detail"].startswith("Heavy rain and strong winds"))
        # a bare subject (or only the system's name): the body's opening sentence becomes the subject
        for subject in ("Flood Advisory", "AlertSF:", ""):
            ev = f.event_for(f.rules[0], {"id": "x", "subject": subject, "excerpt": self.ALERTSF})
            self.assertEqual(ev["subject"], "Flood Advisory in effect until November 20", subject)
            self.assertTrue(ev["detail"].startswith("Heavy rain and strong winds"), subject)

    # San Mateo County (Rave): a headline line, then the notice (as Mail renders the message).
    SMC_CLOSED = ("\nSMC Alert:\nRoad Closure Notice – Industrial Road, San Carlos\nDue to flooding, Industrial Road is "
                  "closed between Harbor Boulevard and Quarry Road until further notice.\nPlease avoid the affected area "
                  "and use alternate routes. Allow extra travel time and follow posted detour signs.\nThank you for your "
                  "patience and cooperation.\n\n￼\n\xa0 \n\xa0\nClick the following link to unsubscribe.")
    SMC_OPEN = ("\nSMC Alert:\nRoad Reopening Notice – Industrial Road, San Carlos\nIndustrial Road has been reopened "
                "between Harbor Boulevard and Quarry Road.\nThank you for your patience and cooperation.\n\n￼\n"
                "Click the following link to unsubscribe.")

    def test_headline_line_is_not_glued_to_the_notice(self):
        """Regression: spoken as "Road Closure Industrial Road, San Carlos. Road Closure Notice – Industrial
        Road, San Carlos Due to flooding, ..." (the headline ran into the next line's sentence)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outside.MailWatch.CONTEXT = Path(tmp.name) / "context.json"
        outside.MailWatch.WATCH = Path(tmp.name) / "mail-watch.json"
        f = outside.MailWatch({"rules": [{"name": "SMC Alert", "sender": "smc.example", "kind": "county_alert",
                                          "subject_strip": r"^(?:smc alert:\s*|\d{4}_)"}]}, HOME)
        ev = f.event_for(f.rules[0], {"id": "a", "subject": "Road Closure Industrial Road, San Carlos",
                                      "excerpt": self.SMC_CLOSED})
        self.assertEqual(ev["subject"], "Road Closure Industrial Road, San Carlos")
        self.assertEqual(ev["detail"], "Due to flooding, Industrial Road is closed between Harbor Boulevard and Quarry "
                                       "Road until further notice. Please avoid the affected area and use alternate routes.")
        ev = f.event_for(f.rules[0], {"id": "b", "subject": "Road Reopening Notice – Industrial Road, San Carlos",
                                      "excerpt": self.SMC_OPEN})
        self.assertEqual(ev["subject"], "Road Reopening Notice – Industrial Road, San Carlos")
        self.assertEqual(ev["detail"], "Industrial Road has been reopened between Harbor Boulevard and Quarry Road. "
                                       "Thank you for your patience and cooperation.")
        # no subject: the headline becomes the subject
        ev = f.event_for(f.rules[0], {"id": "c", "subject": "SMC Alert:", "excerpt": self.SMC_CLOSED})
        self.assertEqual(ev["subject"], "Road Closure Notice – Industrial Road, San Carlos")
        self.assertTrue(ev["detail"].startswith("Due to flooding, Industrial Road is closed"))

    def test_a_wrapped_sentence_stays_one_sentence(self):
        summ = outside.MailWatch.summarize
        self.assertEqual(summ("The road is closed between Harbor Boulevard and\nQuarry Road until further notice."),
                         "The road is closed between Harbor Boulevard and Quarry Road until further notice.")
        self.assertEqual(summ("Crews expect to reopen the road by\nTuesday. Drive safely."),
                         "Crews expect to reopen the road by Tuesday. Drive safely.")


class EmergencyRuleTests(unittest.TestCase):
    def test_event_filters(self):
        alerter = jarvis.Alerter(jarvis.load_config(Path(__file__).resolve().parent.parent / "config.example.yaml"))
        now = dt.datetime(2026, 11, 16, 3, 0)
        evac = {"kind": "weather_alert", "event": "Evacuation Immediate", "severity": "Extreme"}
        wind = {"kind": "weather_alert", "event": "Wind Advisory", "severity": "Moderate"}
        hit = lambda ev: [r["name"] for r in alerter.rules if alerter.matches(r, ev, now)]
        self.assertEqual(hit(evac), ["emergency-message"])
        self.assertEqual(hit(wind), ["weather-alert"])
        self.assertTrue(next(r for r in alerter.rules if r["name"] == "emergency-message").get("urgent"))


if __name__ == "__main__":
    unittest.main()
