"""
tests/test_release_gates.py — Intégration CI.

  pytest -m "not release_gate"   à chaque commit : le banc doit rester fiable (auto-test)
  pytest -m release_gate         avant toute release (bêta comprise) : barrières bloquantes
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.evaluate_pipeline import main  # noqa: E402


def test_harness_detects_every_known_corruption():
    assert main(["--self-test-only"]) == 0


@pytest.mark.release_gate
def test_release_gates(tmp_path):
    report = tmp_path / "benchmark_report.json"
    code = main(["--report", str(report)])
    data = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0, f"Release bloquée : {data['blocking_failures']}"
