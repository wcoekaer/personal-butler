"""Alerts by SMS through ClickSend.

Every alert Jarvis speaks can also go out as a text message: alert rules and
persistent conditions (and their "resolved" follow-ups). Scheduled briefings
never do; messages other systems post on the say topic only with `say: true`.

Credentials come from .env: CLICKSEND_USERNAME (the ClickSend account username,
usually its email address) and CLICKSEND_API_KEY (Dashboard > Developers > API
credentials). Recipients and filters live under `sms:` in config.yaml.

Texts are sent in the background: a slow or failing SMS never delays or stops
the spoken alert. Quiet mode, rule cooldowns and urgency apply exactly as they
do for speech; on top of that the relay drops repeats of the same text and caps
the number of texts per hour (cost guard).
"""

from __future__ import annotations

import asyncio
import base64
import collections
import logging
import os
import time
from typing import Dict, List, Optional

log = logging.getLogger("jarvis.sms")

CLICKSEND_URL = "https://rest.clicksend.com/v3/sms/send"

# One character outside the GSM-7 alphabet switches the whole message to UCS-2,
# which cuts a message part from 160 to 70 characters (and multiplies the cost).
_GSM_SAFE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
             "…": "...", "°": " deg", " ": " ", "•": "-", "·": "-"}


def gsm_text(text: str) -> str:
    for a, b in _GSM_SAFE.items():
        text = text.replace(a, b)
    return " ".join(text.split())


def shorten(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:max(1, limit - 3)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "..."


def mask(number) -> str:
    """Phone numbers in logs: keep the last four digits only."""
    s = str(number or "")
    return ("..." + s[-4:]) if len(s) > 4 else s


class ClickSend:
    """Minimal ClickSend REST v3 client (POST /v3/sms/send, HTTP basic auth)."""

    def __init__(self, username: str, api_key: str, sender: Optional[str] = None,
                 source: str = "jarvis", timeout: float = 20):
        self.username, self.api_key = username, api_key
        self.sender, self.source, self.timeout = sender, source, timeout

    def payload(self, to: List[str], body: str) -> dict:
        def one(number):
            m = {"source": self.source, "body": body, "to": number}
            if self.sender:
                m["from"] = self.sender
            return m
        return {"messages": [one(n) for n in to]}

    def headers(self) -> Dict[str, str]:
        token = base64.b64encode(("%s:%s" % (self.username, self.api_key)).encode()).decode()
        return {"Authorization": "Basic " + token, "Content-Type": "application/json"}

    @staticmethod
    def problems(status: int, data) -> List[str]:
        """Empty when ClickSend accepted every message; otherwise readable reasons."""
        if not isinstance(data, dict):
            return ["HTTP %d" % status]
        if status != 200 or data.get("response_code") != "SUCCESS":
            return ["%s: %s" % (data.get("response_code") or "HTTP %d" % status, data.get("response_msg") or "")]
        out = []
        for m in (data.get("data") or {}).get("messages") or []:
            if m.get("status") != "SUCCESS":
                out.append("%s: %s" % (mask(m.get("to")), m.get("status")))
        return out

    async def send(self, to: List[str], body: str) -> List[str]:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
            async with s.post(CLICKSEND_URL, json=self.payload(to, body), headers=self.headers()) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                return self.problems(r.status, data)


class SmsRelay:
    """Decides which spoken alerts become texts, formats them and sends them."""

    def __init__(self, cfg: Optional[dict], assistant: str = "Hearth", client=None, clock=time.time):
        cfg = cfg or {}
        self.cfg, self.clock = cfg, clock
        to = cfg.get("to") or []
        self.to = [str(n).strip() for n in (to if isinstance(to, list) else [to]) if str(n).strip()]
        self.prefix = str(cfg.get("prefix", "{assistant}: ")).replace("{assistant}", assistant)
        self.max_chars = int(cfg.get("max_chars", 306))          # 2 message parts
        self.max_per_hour = int(cfg.get("max_per_hour", 20))
        self.dedupe_s = float(cfg.get("dedupe_minutes", 10)) * 60
        self.only = set(cfg.get("only") or [])
        self.exclude = set(cfg.get("exclude") or [])
        self.client = client
        if self.client is None:
            user, key = os.environ.get("CLICKSEND_USERNAME"), os.environ.get("CLICKSEND_API_KEY")
            if user and key:
                self.client = ClickSend(user, key, cfg.get("from") or None, str(cfg.get("source", "jarvis")))
        self.enabled = bool(cfg.get("enabled")) and bool(self.to)
        if self.enabled and self.client is None:
            log.warning("sms enabled but CLICKSEND_USERNAME / CLICKSEND_API_KEY are not set in .env; not texting")
            self.enabled = False
        self.sent_times = collections.deque()          # for max_per_hour
        self.last_body: Dict[str, float] = {}           # body -> time sent (dedupe)
        self.last_key: Dict[str, float] = {}            # rule cooldown key -> time sent
        self.capped_logged = 0.0
        self._tasks = set()

    # -- policy ---------------------------------------------------------------
    def wants(self, kind: str, name: str, quiet: bool) -> bool:
        if not self.enabled:
            return False
        if kind == "say" and not self.cfg.get("say", False):
            return False
        if kind in ("condition", "condition_resolved") and not self.cfg.get("conditions", True):
            return False
        if name in self.exclude or (self.only and name not in self.only):
            return False
        if quiet and not self.cfg.get("during_quiet", False):
            return False
        return True

    def decide(self, kind: str, name: str, text: str, quiet: bool = False,
               key: Optional[str] = None, cooldown: float = 0) -> Optional[str]:
        """The message to send for this alert, or None. Records it as sent."""
        if not self.wants(kind, name, quiet):
            return None
        now = self.clock()
        if key and cooldown and now - self.last_key.get(key, -1e12) < cooldown:
            return None
        body = shorten(gsm_text(self.prefix + text), self.max_chars)
        if now - self.last_body.get(body, -1e12) < self.dedupe_s:
            log.info("sms %s skipped (same text sent recently)", name)
            return None
        while self.sent_times and now - self.sent_times[0] > 3600:
            self.sent_times.popleft()
        if self.max_per_hour and len(self.sent_times) >= self.max_per_hour:
            if now - self.capped_logged > 3600:
                self.capped_logged = now
                log.warning("sms limit reached (%d per hour); further alerts are not texted this hour",
                            self.max_per_hour)
            return None
        self.sent_times.append(now)
        self.last_body = {b: t for b, t in self.last_body.items() if now - t < self.dedupe_s}
        self.last_body[body] = now
        if key:
            self.last_key[key] = now
        return body

    # -- sending ----------------------------------------------------------------
    def forward(self, kind: str, name: str, text: str, quiet: bool = False,
                key: Optional[str] = None, cooldown: float = 0) -> Optional[str]:
        """Text this alert in the background if the policy allows it."""
        body = self.decide(kind, name, text, quiet, key, cooldown)
        if body:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return body
            task = loop.create_task(self.deliver(body, name))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return body

    async def deliver(self, body: str, name: str = "test", to: Optional[List[str]] = None) -> bool:
        to = to or self.to
        try:
            problems = await self.client.send(to, body)
        except Exception as e:  # network trouble must never affect the spoken alert
            log.warning("sms %s failed: %s: %s", name, type(e).__name__, e)
            return False
        if problems:
            log.warning("sms %s not delivered: %s", name, "; ".join(problems))
            return False
        log.info("sms %s sent to %s", name, ", ".join(mask(n) for n in to))
        return True


async def send_test(cfg: Optional[dict], assistant: str, text: str, to: Optional[List[str]] = None) -> int:
    """`jarvis sms "text"`: send one text now, ignoring the alert filters."""
    relay = SmsRelay(dict(cfg or {}, enabled=True), assistant)
    numbers = to or relay.to
    if relay.client is None:
        print("CLICKSEND_USERNAME and CLICKSEND_API_KEY must be set in .env")
        return 1
    if not numbers:
        print("no recipient: set sms.to in config.yaml or pass --to")
        return 1
    body = shorten(gsm_text(relay.prefix + text), relay.max_chars)
    ok = await relay.deliver(body, "test", numbers)
    print(("sent to %s" if ok else "NOT sent to %s (see the log line above)") % ", ".join(mask(n) for n in numbers))
    return 0 if ok else 1
