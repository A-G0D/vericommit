"""Helpers to build throwaway git repos for tests and demos."""

from __future__ import annotations

import os
import subprocess

BUGGY = '''def add(a, b):
    """Return the sum of a and b."""
    return a - b
'''
FIXED = '''def add(a, b):
    """Return the sum of a and b."""
    return a + b
'''
TEST = """import unittest
from app.calc import add


class TestAdd(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_negative(self):
        self.assertEqual(add(-2, -3), -5)


if __name__ == "__main__":
    unittest.main()
"""


def make_demo_repo(path: str) -> str:
    os.makedirs(os.path.join(path, "app"), exist_ok=True)
    os.makedirs(os.path.join(path, "tests"), exist_ok=True)
    files = {
        "app/__init__.py": "",
        "app/calc.py": BUGGY,
        "tests/__init__.py": "",
        "tests/test_calc.py": TEST,
        "README.md": "# demo\n\nA tiny repo for VeriCommit demos.\n",
    }
    for rel, content in files.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=demo", "-c", "user.email=demo@localhost", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return path
