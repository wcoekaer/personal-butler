import asyncio
import unittest

import feeds

HOME = (37.0, -122.0)


def target(vid, miles_north, msl_ft, **kw):
    return dict({"vehicle_id": vid, "latitude": HOME[0] + miles_north / 69.0, "longitude": HOME[1],
                 "altitude_ft": msl_ft, "source_protocol": "adsb_1090", "callsign": vid}, **kw)


class LowAircraftTiers(unittest.TestCase):
    """Red tier (announced) is unchanged; a display-only orange tier lists other aircraft
    within display_radius_miles below display_max_altitude_ft_agl, never announced."""

    def poll(self, targets):
        f = feeds.LowAircraft.__new__(feeds.LowAircraft)
        f.cfg = {"radius_miles": 2, "max_altitude_ft_agl": 1200, "home_elevation_ft": 800,
                 "display_radius_miles": 3, "display_max_altitude_ft_agl": 4000}
        f.home, f.interval, f.primed, f.journal, f.state, f.events = HOME, 30, True, None, {}, []
        f.listeners = [f.events.append]

        async def get_json(session, url, **kw):
            return {"targets": targets}
        f.get_json = get_json
        import os
        os.environ.setdefault("AIRSPACE_URL", "http://x")
        os.environ.setdefault("AIRSPACE_API_KEY", "k")
        asyncio.run(f.poll(None))
        return f

    def test_tiers(self):
        f = self.poll([
            target("LOW1", 1.0, 1800),            # 1000 ft AGL, 1 mi: red, announced
            target("MID1", 1.5, 3500),            # 2700 ft AGL, 1.5 mi: orange
            target("MID2", 2.5, 1500),            # 700 ft AGL but 2.5 mi: orange (outside the red radius)
            target("HIGH", 1.0, 6000),            # 5200 ft AGL: neither
            target("FAR", 4.0, 2000),             # 4 mi: neither
            target("GND", 2.8, 0, on_ground=True),
        ])
        self.assertEqual([e["callsign"] for e in f.events if e["kind"] == "low_aircraft"], ["LOW1"])
        self.assertEqual(sorted(n["label"] for n in f.state["nearby"]), ["MID1", "MID2"])
        self.assertTrue(all(n["dir"] == "north" for n in f.state["nearby"]))


if __name__ == "__main__":
    unittest.main()
