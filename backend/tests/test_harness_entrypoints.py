"""Production entry points must work without pytest's API-first import order."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "teaching_assistant.agent.worker",
        "teaching_assistant.harness.worker",
        "teaching_assistant.harness.models",
        "teaching_assistant.harness.assisted_lesson",
    ],
)
def test_worker_and_adapter_import_in_fresh_process(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
