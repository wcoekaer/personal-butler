import datetime as dt
import json
import time
import unittest

import vehicles

TOPIC = "teslaapi/vehicle_data_car1"
CFG = {"mqtt": {"topics": [{"topic": TOPIC, "field": "response.charge_state.battery_level", "label": "Rover battery"}]}}


def payload(status="", version=" ", car_version="2026.38.5.13", front=2.95, rear=2.95, rcp=3.45, age_s=3600, warn=False):
    now = time.time()
    vs = {"car_version": car_version,
          "software_update": {"status": status, "version": version, "expected_duration_sec": 2700},
          "tpms_rcp_front_value": rcp, "tpms_rcp_rear_value": rcp}
    for w in ("fl", "fr", "rl", "rr"):
        vs["tpms_pressure_" + w] = front if w[0] == "f" else rear
        vs["tpms_last_seen_pressure_time_" + w] = now - age_s
        vs["tpms_soft_warning_" + w] = warn and w == "rr"
    return {TOPIC: (json.dumps({"response": {"vehicle_state": vs}}), dt.datetime.now())}


class VehicleChecks(unittest.TestCase):
    def setUp(self):
        self.mon = vehicles.VehicleMonitor(CFG)

    def test_names_come_from_battery_labels(self):
        self.assertEqual(self.mon.names, {TOPIC: "Rover"})

    def test_update_announced_once_per_version(self):
        ok = payload(front=3.45, rear=3.45)
        self.assertEqual(self.mon.check(ok), [])
        ev = self.mon.check(payload("available", "2026.44.1", front=3.45, rear=3.45))
        self.assertEqual([(e["kind"], e["version"], e["status_text"], e["duration"]) for e in ev],
                         [("vehicle_update", "2026.44.1", "is ready to install", "about 45 minutes")])
        self.assertEqual(self.mon.check(payload("scheduled", "2026.44.1", front=3.45, rear=3.45)), [])
        self.assertEqual(self.mon.flags(TOPIC), {"update": "2026.44.1"})
        self.assertIn("software update 2026.44.1", self.mon.lines()[0])
        self.mon.check(payload("", " ", car_version="2026.44.1 abc", front=3.45, rear=3.45))   # installed
        self.assertEqual(self.mon.flags(TOPIC), {})
        ev = self.mon.check(payload("available", "2026.50.2", front=3.45, rear=3.45))
        self.assertEqual([e["version"] for e in ev], ["2026.50.2"])

    def test_low_tires_announced_once_and_rearmed_after_recovery(self):
        ev = self.mon.check(payload())
        self.assertEqual(ev[0]["kind"], "vehicle_tires_low")
        self.assertEqual(ev[0]["detail"], "all four tires are at 43 psi, against a recommended 50 psi")
        self.assertEqual(self.mon.check(payload()), [])
        self.assertTrue(self.mon.flags(TOPIC)["tires_low"])
        self.assertEqual(self.mon.check(payload(front=3.2, rear=3.2)), [])          # 93 %: not low, not re-armed
        self.assertEqual(self.mon.flags(TOPIC), {})
        self.assertEqual(self.mon.check(payload()), [])                             # still the same episode
        self.mon.check(payload(front=3.4, rear=3.4))                                # 99 %: re-armed
        self.assertEqual(len(self.mon.check(payload())), 1)

    def test_one_low_tire_warning_and_stale_readings(self):
        ev = self.mon.check(payload(front=3.45, rear=3.40, warn=True))
        self.assertEqual(ev[0]["detail"], "the rear right tire is at 49 psi, against a recommended 50 psi")
        fresh = vehicles.VehicleMonitor(CFG)
        self.assertEqual(fresh.check(payload(age_s=30 * 86400)), [])


if __name__ == "__main__":
    unittest.main()
