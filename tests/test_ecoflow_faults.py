import asyncio
import copy
import datetime as dt
import unittest

import feeds

BASE = {"code": "0", "data": {"ems.lcdShowSoc": 79, "inv.acInVol": 120000,
                              "pd.errCode": 0, "inv.errCode": 0, "mppt.faultCode": 0}}


def quota(**errors):
    resp = copy.deepcopy(BASE)
    resp["data"].update({k.replace("_", "."): v for k, v in errors.items()})
    return resp


def feed(cfg=None, state=None):
    f = feeds.EcoFlow.__new__(feeds.EcoFlow)
    f.cfg = dict({"sn": "X", "name": "DELTA Pro"}, **(cfg or {}))
    f.home, f.interval, f.primed, f.journal = (0, 0), 60, True, None
    f.state, f.events = state if state is not None else {}, []
    f.listeners = [f.events.append]
    f._signed_headers = lambda params: {}
    return f


def poll(f, *responses):
    for resp in responses:
        async def get_json(*a, _resp=resp, **k):
            return _resp
        f.get_json = get_json
        asyncio.run(f.poll(None))
    return [e["detail"] for e in f.events if e["kind"] == "ecoflow_fault"]


class EcoFlowFaults(unittest.TestCase):
    """Regression: an extra battery's errCode drops in and out of quota/all, and each
    reappearance was announced as a new fault (18 events in an hour on 2026-11-15)."""

    FLAP = [quota(bmsMaster_errCode=23, bmsSlave2_errCode=23), quota(bmsMaster_errCode=23),
            quota(bmsMaster_errCode=23, bmsSlave2_errCode=23), quota(bmsMaster_errCode=23)]

    def test_flapping_field_is_announced_once(self):
        f = feed()
        self.assertEqual(poll(f, *self.FLAP), ["bmsMaster.errCode 23, bmsSlave2.errCode 23"])
        self.assertEqual(f.state["current"]["errors"], {"bmsMaster.errCode": 23, "bmsSlave2.errCode": 23})

    def test_acknowledged_codes_are_silent_and_hidden(self):
        f = feed({"acknowledged_faults": {"bmsMaster.errCode": 23, "bmsSlave2.errCode": 23}})
        self.assertEqual(poll(f, *self.FLAP), [])
        self.assertEqual(f.state["current"]["errors"], {})
        self.assertTrue(f.lines(dt.datetime.now())[0].endswith("no faults"))
        self.assertEqual(poll(f, quota(bmsMaster_errCode=31)), ["bmsMaster.errCode 31"])

    def test_explicit_zero_rearms(self):
        f = feed()
        alerts = poll(f, quota(bmsMaster_errCode=23), quota(bmsMaster_errCode=0), quota(bmsMaster_errCode=23))
        self.assertEqual(alerts, ["bmsMaster.errCode 23", "bmsMaster.errCode 23"])

    def test_restart_does_not_reannounce(self):
        f = feed(state={"current": {"errors": {"bmsMaster.errCode": 23}, "ac_volts": 120}})
        self.assertEqual(poll(f, quota(bmsMaster_errCode=23)), [])


if __name__ == "__main__":
    unittest.main()
