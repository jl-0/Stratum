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

TERRAFORM_STUB = f"""#!/usr/bin/env bash
case "$*" in
  *"output -raw viewer_cluster"*) printf stratum-test ;;
  *"output -raw viewer_task_definition"*) printf '{VIEWER_ARN}' ;;
  *"output -raw viewer_image_uri"*) printf 'repo@sha256:abc' ;;
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
