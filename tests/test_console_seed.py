"""The console page ships with the repository's configuration embedded for demo mode; keep it current."""

import os
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))

from build_console_seed import CONSOLE, build_seed, render  # noqa: E402

from orchestrator.console.api import console_page  # noqa: E402


class ConsoleSeedTests(unittest.TestCase):
    def test_seed_is_current(self):
        html = CONSOLE.read_text(encoding="utf-8")
        self.assertEqual(render(html, build_seed()), html,
                         "run: PYTHONPATH=src python scripts/build_console_seed.py")

    def test_page_shell(self):
        page = console_page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>Agent Command Center</title>", page)
        self.assertIn('"evals":[', page)
        self.assertNotIn("</script><script", page.split("DEMO_SEED_START")[1].split("DEMO_SEED_END")[0])


if __name__ == "__main__":
    unittest.main()
