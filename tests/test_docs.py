"""Documentation stays in sync with the code."""

import os
import subprocess
import sys
import unittest

from helpers import ROOT


class ConfigReferenceTests(unittest.TestCase):
    def test_every_key_documented_and_reference_current(self):
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "gen_config_reference.py")],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("MISSING", out, "add descriptions for new keys in scripts/gen_config_reference.py")
        with open(os.path.join(ROOT, "docs", "CONFIG_REFERENCE.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), out, "regenerate docs/CONFIG_REFERENCE.md")


if __name__ == "__main__":
    unittest.main()
