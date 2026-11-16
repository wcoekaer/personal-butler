"""Tesla vehicle checks on the vehicle_data payloads already on the home broker
(teslaapi/vehicle_data_<id>, see mqtt.topics): a software update ready to
install, and tire pressure below the car's own recommended cold pressure.

Each condition is announced once (an update once per version; low tires once
until they are back within half the threshold) and listed in briefings while
it lasts. Car names come from the "<Name> battery" labels in mqtt.topics.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

log = logging.getLogger("jarvis.vehicles")

BAR_TO_PSI = 14.5038
WHEELS = {"fl": "front left", "fr": "front right", "rl": "rear left", "rr": "rear right"}
READY = {"available": "is ready to install", "scheduled": "is scheduled to install"}


def _get(d: dict, path: str):
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _join(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class VehicleMonitor:
    def __init__(self, cfg: dict, journal=None):
        vcfg = cfg.get("vehicles") or {}
        self.journal = journal
        self.low = float(vcfg.get("tire_low_percent", 10)) / 100.0
        self.max_age_s = 86400 * float(vcfg.get("tire_max_age_days", 14))
        self.names: Dict[str, str] = {}
        for spec in (cfg.get("mqtt") or {}).get("topics", []):
            topic, field = str(spec.get("topic", "")), str(spec.get("field", ""))
            if topic.startswith("teslaapi/vehicle_data_") and field.endswith("battery_level"):
                self.names[topic] = str(spec.get("label", topic)).rsplit(" battery", 1)[0]
        self.state: Dict[str, dict] = ((journal.get("vehicles:state") if journal else None) or {})

    def check(self, values: Dict[str, tuple], now: Optional[float] = None) -> List[dict]:
        """values: MqttState.values (topic -> (payload, seen)); returns events."""
        now = time.time() if now is None else now
        events, changed = [], False
        for topic, name in self.names.items():
            if topic not in values:
                continue
            try:
                r = (json.loads(values[topic][0]) or {}).get("response") or {}
            except (ValueError, TypeError, AttributeError):
                continue
            vs = r.get("vehicle_state") or {}
            if not vs:
                continue
            st = self.state.setdefault(topic, {})
            # --- software update ready ---------------------------------------------
            su = vs.get("software_update") or {}
            status, version = str(su.get("status") or ""), str(su.get("version") or "").strip()
            if status in READY and version:
                st["update"] = {"version": version, "status": status,
                                "minutes": int(round((su.get("expected_duration_sec") or 0) / 60))}
                if st.get("update_announced") != version:
                    st["update_announced"] = version
                    mins = st["update"]["minutes"]
                    events.append({"kind": "vehicle_update", "device": name, "vehicle": name,
                                   "version": version, "status_text": READY[status],
                                   "duration": ("about %d minutes" % mins) if mins else "an unknown time"})
                changed = True
            elif st.get("update") and (not status or vs.get("car_version", "").startswith(st["update"]["version"])):
                st.pop("update", None)          # installed (or withdrawn); a new version announces again
                changed = True
            # --- tire pressure ---------------------------------------------------------
            low, ok = [], True
            for w, label in WHEELS.items():
                p = vs.get("tpms_pressure_" + w)
                rcp = vs.get("tpms_rcp_front_value" if w[0] == "f" else "tpms_rcp_rear_value")
                seen = vs.get("tpms_last_seen_pressure_time_" + w) or 0
                if not isinstance(p, (int, float)) or not isinstance(rcp, (int, float)) or rcp <= 0:
                    continue
                if seen and now - float(seen) > self.max_age_s:
                    continue
                warned = bool(vs.get("tpms_soft_warning_" + w) or vs.get("tpms_hard_warning_" + w))
                if warned or p < rcp * (1 - self.low):
                    low.append((label, p * BAR_TO_PSI, rcp * BAR_TO_PSI))
                if warned or p < rcp * (1 - self.low / 2):
                    ok = False
            if low:
                st["tires"] = [[l, round(p, 1), round(r, 1)] for l, p, r in low]
                if not st.get("tires_announced"):
                    st["tires_announced"] = True
                    events.append({"kind": "vehicle_tires_low", "device": name, "vehicle": name,
                                   "detail": self._tire_detail(low)})
                changed = True
            else:
                if st.pop("tires", None) is not None:
                    changed = True
                if ok and st.get("tires_announced"):
                    st["tires_announced"] = False       # back within half the threshold: re-armed
                    changed = True
        if changed and self.journal:
            self.journal.put("vehicles:state", self.state)
        return events

    @staticmethod
    def _tire_detail(low) -> str:
        recs = sorted({round(r) for _, _, r in low})
        rec = "%d psi" % recs[0] if len(recs) == 1 else "%d to %d psi" % (recs[0], recs[-1])
        lo, hi = min(round(p) for _, p, _ in low), max(round(p) for _, p, _ in low)
        at = ("%d psi" % lo) if lo == hi else "%d to %d psi" % (lo, hi)
        where = "all four tires are" if len(low) == 4 else (
            "the %s tire is" % low[0][0] if len(low) == 1 else "the %s tires are" % _join([l for l, _, _ in low]))
        return "%s at %s, against a recommended %s" % (where, at, rec)

    def flags(self, topic: str) -> dict:
        st = self.state.get(topic) or {}
        out = {}
        if st.get("update"):
            out["update"] = st["update"]["version"]
        if st.get("tires"):
            out["tires_low"] = True
        return out

    def lines(self) -> List[str]:
        out = []
        for topic, name in self.names.items():
            st = self.state.get(topic) or {}
            if st.get("update"):
                u = st["update"]
                out.append("- %s: software update %s %s%s" % (
                    name, u["version"], READY.get(u["status"], "is pending"),
                    (", about %d minutes to install" % u["minutes"]) if u.get("minutes") else ""))
            if st.get("tires"):
                low = [(l, p, r) for l, p, r in st["tires"]]
                out.append("- %s tire pressure low: %s" % (name, self._tire_detail(low)))
        return out
