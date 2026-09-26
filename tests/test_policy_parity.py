"""Keeps policies/orchestrator.rego and the local Python engine in step."""

import ast
import os
import re
import unittest

from helpers import ROOT


class ParityTests(unittest.TestCase):
    def test_same_deny_messages(self):
        rego = open(os.path.join(ROOT, "policies", "orchestrator.rego"), encoding="utf-8").read()
        rego_msgs = set(re.findall(r'deny contains "([^"]+)"', rego))
        src = open(os.path.join(ROOT, "src", "orchestrator", "policy.py"), encoding="utf-8").read()
        func = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "local_deny_reasons")
        py_msgs = {
            n.args[0].value for n in ast.walk(func)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "append" and isinstance(n.args[0], ast.Constant)
        }
        py_msgs |= {"agent not registered"}  # returned directly rather than appended
        self.assertEqual(rego_msgs, py_msgs)


if __name__ == "__main__":
    unittest.main()


class ChartPolicyTests(unittest.TestCase):
    def test_helm_chart_policy_matches(self):
        src = open(os.path.join(ROOT, "policies", "orchestrator.rego"), encoding="utf-8").read()
        chart = open(os.path.join(ROOT, "deploy", "helm", "orchestrator", "files", "orchestrator.rego"), encoding="utf-8").read()
        self.assertEqual(src, chart, "run `make helm-sync` to copy the policy into the chart")
