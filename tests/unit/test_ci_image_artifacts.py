"""Offline contracts for the tested-image handoff; no Docker or cloud access."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
CONTAINER = WORKFLOW["jobs"]["container"]
STEPS = {step["name"]: step for step in CONTAINER["steps"]}
EXPORT = STEPS["Export the tested candidate images"]
UPLOAD = STEPS["Retain candidate image package"]


@pytest.mark.parametrize("successful", [False, True])
@pytest.mark.parametrize(
    ("event", "ref", "eligible"),
    [
        ("push", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/main", True),
        ("pull_request", "refs/pull/1/merge", False),
        ("pull_request", "refs/heads/main", False),
        ("push", "refs/heads/codex/task", False),
        ("workflow_dispatch", "refs/heads/codex/task", False),
        ("push", "refs/tags/release", False),
        ("workflow_run", "refs/heads/main", False),
    ],
)
def test_image_archive_requires_successful_main_event(successful, event, ref, eligible):
    assert EXPORT["if"] == UPLOAD["if"]
    # Evaluate this repository-owned GitHub expression against the event boundary.
    expression = EXPORT["if"].replace("&&", " and ").replace("||", " or ")
    actual = eval(
        expression,
        {"__builtins__": {}},
        {"github": SimpleNamespace(event_name=event, ref=ref), "success": lambda: successful},
    )
    assert actual is (successful and eligible)


def test_image_build_and_archive_wait_for_every_test_gate():
    jobs = WORKFLOW["jobs"]
    assert set(CONTAINER["needs"]) == set(jobs) - {"container"}
    assert "if" not in CONTAINER  # Default success() must reject failed/skipped prerequisites.
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_archive_retains_tested_images_checksum_and_revision():
    script = EXPORT["run"]
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
    assert "set -euo pipefail" in script
    assert "docker save events-concierge:ci events-concierge-web:ci" in script
    assert "sha256sum images.tar.gz > SHA256SUMS" in script
    assert "printf '%s\\n' \"${GITHUB_SHA}\" > SOURCE_REVISION" in script
    assert UPLOAD["with"]["name"] == "candidate-images-${{ github.sha }}"
    assert UPLOAD["with"]["retention-days"] == 3
    assert UPLOAD["with"]["if-no-files-found"] == "error"
    names = [step["name"] for step in CONTAINER["steps"]]
    assert names.index("Run non-mutating candidate-stack canary") < names.index(EXPORT["name"])
    assert names.index("Verify every candidate process remains running") < names.index(EXPORT["name"])
