import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

import hud
import jarvis
import operations
from journal import Journal

ROOT = Path(__file__).resolve().parent.parent


class AssistantName(unittest.TestCase):
    def cfg(self, name=None):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            text = (ROOT / "config.example.yaml").read_text()
            if name:
                text = text.replace("assistant_name: Hearth", "assistant_name: %s" % name, 1)
            f.write(text)
        return jarvis.load_config(Path(f.name))

    def test_neutral_default_and_override_reach_persona_and_prompts(self):
        c = self.cfg()
        self.assertEqual(c["assistant_name"], "Hearth")
        self.assertIn("You are Hearth,", jarvis.speech_instructions(c["llm"]))
        self.assertNotIn("Jarvis", jarvis.speech_instructions(c["llm"]))
        mine = self.cfg("Jarvis")
        self.assertIn("You are Jarvis,", jarvis.speech_instructions(dict(mine["llm"], _structured=True)))
        prompt = jarvis.render_prompt("Speak as {assistant_name}.", dt.datetime(2026, 11, 16, 9), mine)
        self.assertIn("Speak as Jarvis.", prompt)

    def test_example_config_is_neutral(self):
        c = self.cfg()
        self.assertTrue(all(s["name"].startswith("Hearth ") for s in c["homekit"]["switches"]))
        for b in c["broadcasts"]:
            self.assertNotIn("Jarvis", b.get("prompt", ""))

    def test_hud_status_and_dashboard_carry_the_name(self):
        c = self.cfg("Jarvis")
        self.assertEqual(hud.build_status(jarvis, c)["assistant"], "Jarvis")
        self.assertIn("HEARTH", (ROOT / "hud" / "index.html").read_text())     # neutral default before data
        with tempfile.TemporaryDirectory() as tmp:
            store = Journal(Path(tmp) / "journal.db")
            server = operations.dashboard_server(store, 0, "Jarvis")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                page = urlopen("http://127.0.0.1:%d/" % server.server_port).read().decode()
                self.assertIn("<title>Jarvis · Home journal</title>", page)
                self.assertIn("JARVIS / HOUSEHOLD JOURNAL", page)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
