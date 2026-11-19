"""The operator scripts under `scripts/`, run against stub `aws` and `terraform` executables.

Nothing here reaches AWS: the stubs sit first on `PATH`, record the arguments they were called
with and answer from a fixture. What is checked is the command the script would have run.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
VIEWER_ARN = "arn:aws:ecs:us-west-2:000000000000:task-definition/stratum-test-viewer:7"
VIEWER_TASK = "arn:aws:ecs:us-west-2:000000000000:task/stratum-test/viewer0001"
MOSAIC_TASK = "arn:aws:ecs:us-west-2:000000000000:task/stratum-test/mosaic0001"
RUN_ARN = "arn:aws:ecs:us-west-2:000000000000:task-definition/stratum-test-run:3"
RUN_TASK = "arn:aws:ecs:us-west-2:000000000000:task/stratum-test/run0001"

TERRAFORM_STUB = f"""#!/usr/bin/env bash
case "$*" in
  *"output -raw viewer_cluster"*) printf stratum-test ;;
  *"output -raw viewer_task_definition"*) printf '{VIEWER_ARN}' ;;
  *"output -raw viewer_image_uri"*) printf 'repo@sha256:abc' ;;
  *"output -raw run_task_definition"*) printf '{RUN_ARN}' ;;
  *"output -raw storage_root"*) printf 's3://stratum-test-bucket/' ;;
  *"output -raw function_name"*) printf 'stratum-test-worker' ;;
  *"output -json viewer_network"*)
    printf '{{"subnets":["s-1"],"security_groups":["sg-1"],"assign_public_ip":"DISABLED","port":8000}}' ;;
esac
"""

# The cluster holds a viewer task AND a mosaic build, as it does during `make cloud-mosaic`.
# `list-tasks` answers as ECS does: every task, unless a family narrows it.
AWS_STUB = f"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$*" in
  *"ecs list-tasks"*"--family stratum-test-viewer"*) echo "{VIEWER_TASK}" ;;
  *"ecs list-tasks"*"--family"*) ;;
  *"ecs list-tasks"*) echo "{VIEWER_TASK}	{MOSAIC_TASK}" ;;
  *"ecs stop-task"*) echo stopped ;;
  *"ecs run-task"*) echo "{RUN_TASK}" ;;
  *"lambda get-function-concurrency"*) echo 64 ;;
  *"ecs describe-tasks"*) echo eni-1 ;;
  *"ec2 describe-network-interfaces"*) echo 10.0.0.9 ;;
esac
"""


@pytest.fixture
def stubbed(tmp_path: Path) -> dict[str, str]:
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("aws", AWS_STUB), ("terraform", TERRAFORM_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    env_file = tmp_path / ".env"
    env_file.write_text('AWS_PROFILE=test\nAWS_REGION=us-west-2\nSTRATUM_DEPLOYMENT=test\n'
                        'STRATUM_PERMISSIONS_BOUNDARY_ARN=""\nSTRATUM_ROLE_NAME_PREFIX=""\n')
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STRATUM_ENV": str(env_file), "STUB_LOG": str(tmp_path / "aws.log")}


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["STUB_LOG"])
    return log.read_text().splitlines() if log.is_file() else []


def test_viewer_down_stops_only_viewer_tasks(stubbed: dict[str, str]) -> None:
    """The mosaic task borrows the viewer's cluster, so "every RUNNING task in the cluster" is
    not "every viewer task": `make viewer-down` during a long mosaic build stopped the build."""
    done = subprocess.run(["bash", str(SCRIPTS / "viewer-task.sh"), "down"], env=stubbed,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    stopped = [c for c in _calls(stubbed) if "ecs stop-task" in c]
    assert len(stopped) == 1 and VIEWER_TASK in stopped[0]
    assert not any(MOSAIC_TASK in c for c in stopped), "viewer-down stopped the mosaic build"


def test_viewer_url_lists_only_viewer_tasks(stubbed: dict[str, str]) -> None:
    done = subprocess.run(["bash", str(SCRIPTS / "viewer-task.sh"), "url"], env=stubbed,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert VIEWER_TASK in done.stdout and MOSAIC_TASK not in done.stdout


def test_run_task_stages_the_manifest_tree_and_starts_a_detached_run(
        stubbed: dict[str, str], tmp_path: Path) -> None:
    """`make cloud-run-detached`: the manifest's directory and the plain files above it are
    staged under one bundle prefix, the task is handed that prefix and the cloud manifest's path
    inside it, and the script returns without waiting for the run."""
    site = tmp_path / "examples" / "site"
    (site / "index").mkdir(parents=True)
    (site / "out").mkdir()
    (site / "manifest.yaml").write_text("outputs:\n  bucket: ./out\n")
    (tmp_path / "examples" / "zones.yaml").write_text("zones: {}\n")
    done = subprocess.run(["bash", str(SCRIPTS / "run-task.sh"), str(site / "manifest.yaml"),
                           "--workers", "40"], env=stubbed, capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert "bucket: s3://stratum-test-bucket/" in (site / "manifest.cloud.yaml").read_text()
    calls = _calls(stubbed)
    sync = next(c for c in calls if "s3 sync" in c)
    assert "s3://stratum-test-bucket/bundles/manifest-" in sync and sync.count("--exclude") == 2
    assert any("s3 cp" in c and c.rstrip().split()[-2].endswith("/zones.yaml") for c in calls)
    started = next(c for c in calls if "ecs run-task" in c)
    assert RUN_ARN in started
    assert "stratum orchestrate --bundle s3://stratum-test-bucket/bundles/manifest-" in started
    assert "-m site/manifest.cloud.yaml --workers 40;" in started
    assert RUN_TASK in done.stdout and "no longer needs this machine" in done.stdout
    assert not any("describe-tasks" in c for c in calls), "the script waited for the run"


def _launch(stubbed: dict[str, str], tmp_path: Path, *args: str) -> str:
    site = tmp_path / "examples" / "site"
    site.mkdir(parents=True)
    (site / "manifest.yaml").write_text("outputs:\n  bucket: ./out\n")
    done = subprocess.run(["bash", str(SCRIPTS / "run-task.sh"), str(site / "manifest.yaml"),
                           *args], env=stubbed, capture_output=True, text=True, timeout=60,
                          check=False)
    assert done.returncode == 0, done.stderr
    return next(c for c in _calls(stubbed) if "ecs run-task" in c)


def test_run_task_retries_the_whole_run_and_dispatches_at_the_reservation(
        stubbed: dict[str, str], tmp_path: Path) -> None:
    """One upstream outage fails a stage, and so a run, hours in. The task therefore runs the
    run up to three times - a shell loop in the command, not code in the image, so it changes
    nothing a cache key is made from - and never retries a refused budget (exit 2). With no
    --workers the dispatcher is given the function's reserved concurrency."""
    started = _launch(stubbed, tmp_path)
    assert '"bash", "-c", "n=1; until stratum orchestrate --bundle ' in started
    assert "--workers 64; do rc=$?" in started
    assert '[ \\"$rc\\" -eq 2 ] || [ \\"$n\\" -ge 3 ]' in started


def test_run_task_runs_once_when_asked(stubbed: dict[str, str], tmp_path: Path) -> None:
    started = _launch(stubbed, tmp_path, "--attempts", "1", "--workers", "8", "--dry-run")
    assert '["stratum", "orchestrate", "--bundle"' in started and "until" not in started
    assert '"--workers", "8", "--dry-run"]' in started
    assert "get-function-concurrency" not in "\n".join(_calls(stubbed))
