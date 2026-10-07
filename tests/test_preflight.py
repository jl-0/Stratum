"""The run estimate (08 section 7) and the executor preflight checks (08 section 1)."""
from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest
from click.testing import CliRunner
from synthetic_nc import write_manifest, write_scenes

from stratum.cli import main
from stratum.executors import run_all
from stratum.executors.preflight import failed, preflight, wall_hours
from stratum.plan import BudgetExceeded, plan_run
from stratum.plan.estimate import (
    LAMBDA_SLOWDOWN,
    LAMBDA_USD_PER_GB_S,
    estimate,
    lambda_cost,
)

CAL = {"tile_cells": 3600 * 3600, "block_cells": 720 * 720, "bytes_per_cell": 10}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_scenes(tmp_path / "granules")
    return tmp_path


# ------------------------------------------------------------------------------------ estimate
def test_the_model_reproduces_its_calibration_run() -> None:
    """At the calibration geometry the per-item model is the fitted line: regrid 6.56 + 23.44 x
    cover, resolve 0.074 + 0.080 per candidate, reduce 8.65, publish 3.87 seconds."""
    rom = estimate(**CAL, regrid_overlaps=[0.0, 1.0], resolve_candidates=[1, 4],
                   reduce_items=2, publish_items=1)
    assert rom["cpu_seconds"]["regrid"] == pytest.approx(6.56 * 2 + 23.44)
    assert rom["cpu_seconds"]["resolve"] == pytest.approx(0.074 * 2 + 0.080 * 5)
    assert rom["cpu_seconds"]["reduce"] == pytest.approx(8.65 * 2)
    assert rom["cpu_seconds"]["publish"] == pytest.approx(3.87)
    assert rom["longest_item_seconds"]["regrid"] == pytest.approx(30.0)
    assert rom["items"] == {"regrid": 2, "resolve": 2, "reduce": 2, "publish": 1}


def test_cost_scales_with_cells_and_bytes() -> None:
    """Pixels x data type: twice the bytes per cell doubles reduce; a quarter of the block
    quarters it; neither touches regrid, which scales with tile cover."""
    base = estimate(**CAL, regrid_overlaps=[0.5], resolve_candidates=[2], reduce_items=1,
                    publish_items=1)
    wide = estimate(**{**CAL, "bytes_per_cell": 20}, regrid_overlaps=[0.5],
                    resolve_candidates=[2], reduce_items=1, publish_items=1)
    small = estimate(**{**CAL, "block_cells": 360 * 360}, regrid_overlaps=[0.5],
                     resolve_candidates=[2], reduce_items=1, publish_items=1)
    assert wide["cpu_seconds"]["reduce"] == pytest.approx(2 * base["cpu_seconds"]["reduce"])
    assert small["cpu_seconds"]["reduce"] == pytest.approx(base["cpu_seconds"]["reduce"] / 4)
    assert wide["cpu_seconds"]["regrid"] == base["cpu_seconds"]["regrid"]
    assert wide["footprint_bytes"]["snapshots"] == 2 * base["footprint_bytes"]["snapshots"]


def test_lambda_cost_is_gb_seconds_on_slower_cores_plus_requests() -> None:
    cost = lambda_cost(1.0, 1_000_000, 2048)
    gb_s = 3600 * LAMBDA_SLOWDOWN * 2
    assert cost["gb_seconds"] == pytest.approx(gb_s)
    assert cost["usd"][1] == pytest.approx(gb_s * LAMBDA_USD_PER_GB_S + 0.20)
    assert cost["usd"][0] < cost["usd"][1] < cost["usd"][2]


def test_wall_time_is_bounded_below_by_the_longest_item() -> None:
    rom = estimate(**CAL, regrid_overlaps=[1.0], resolve_candidates=[1], reduce_items=1,
                   publish_items=1)
    one = wall_hours(rom, 1)
    assert wall_hours(rom, 1000) == pytest.approx(one)      # one item per stage: no speed-up


def test_plan_writes_the_estimate_and_reports_it(root: Path) -> None:
    result = plan_run(write_manifest(root / "m.yaml"))
    rom = result.document["estimate"]
    assert rom["items"]["regrid"] == result.counts["work_regrid"]
    assert rom["cpu_hours"] > 0 and rom["download_bytes"] is None      # a local source
    assert "## Estimate (rough order of magnitude" in result.report
    assert "vcpu hours: ~" in result.report and "(estimated)" in result.report


def test_max_vcpu_hours_is_enforced_against_the_estimate(root: Path) -> None:
    """09 section 4: the one budget field that could not be checked before there was an
    estimate. The plan is written - work lists and all - and the run refuses it."""
    result = plan_run(write_manifest(root / "m.yaml", budget={
        "max_tiles": 1, "max_granules": 100, "max_vcpu_hours": 1e-6, "on_exceed": "fail"}))
    assert result.over_budget and result.refused
    assert "CPU-hours exceed budget.max_vcpu_hours" in result.budget_problems[0]
    with pytest.raises(BudgetExceeded, match="CPU-hours"):
        run_all(result.run_dir, workers=1)


# ------------------------------------------------------------------------------- local preflight
Usage = namedtuple("Usage", "total used free")


def test_local_preflight_fails_when_the_root_cannot_hold_the_run(root: Path, monkeypatch) -> None:
    doc = plan_run(write_manifest(root / "m.yaml")).document
    assert not failed(preflight(doc, "local"))
    monkeypatch.setattr("stratum.executors.preflight.shutil.disk_usage",
                        lambda p: Usage(10, 10, 10))
    checks = {c.name: c for c in preflight(doc, "local")}
    assert checks["disk: storage root"].level == "fail"
    assert "wall time" in checks


# --------------------------------------------------------------------------------- aws preflight
class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeSTS:
    def __init__(self, error: str | None = None) -> None:
        self.error = error

    def get_caller_identity(self) -> dict:
        if self.error:
            raise ClientError(self.error)
        return {"Account": "123456789012"}


class FakeLambda:
    def __init__(self, timeout: int = 900, reserved: int = 64) -> None:
        self.timeout, self.reserved = timeout, reserved

    def get_function_configuration(self, FunctionName: str) -> dict:
        return {"MemorySize": 4096, "Timeout": self.timeout, "Architectures": ["arm64"],
                "LastModified": "2026-10-01T00:00:00Z"}

    def get_function_concurrency(self, FunctionName: str) -> dict:
        return {"ReservedConcurrentExecutions": self.reserved}


def _rom_doc(longest_s: float = 10.0) -> dict:
    rom = estimate(**CAL, regrid_overlaps=[1.0], resolve_candidates=[1], reduce_items=1,
                   publish_items=1)
    rom["longest_item_seconds"]["regrid"] = longest_s
    return {"estimate": rom, "root": "s3://bucket/", "staging": {}}


def test_aws_preflight_stops_on_expired_credentials() -> None:
    checks = preflight(_rom_doc(), "aws", sts_client=FakeSTS("ExpiredToken"))
    assert [(c.name, c.level) for c in checks] == [("credentials", "fail")]
    assert "ExpiredToken" in checks[0].detail


def test_aws_preflight_checks_the_function_timeout_and_concurrency(monkeypatch) -> None:
    monkeypatch.setenv("STRATUM_LAMBDA_FUNCTION", "stratum-dev-worker")
    ok = {c.name: c for c in preflight(_rom_doc(10.0), "aws", workers=32,
                                        sts_client=FakeSTS(), lambda_client=FakeLambda())}
    assert {n: c.level for n, c in ok.items()} == {
        "credentials": "ok", "worker function": "ok", "timeout": "ok", "concurrency": "ok",
        "cost": "ok"}
    assert "4096 MB" in ok["cost"].detail
    slow = {c.name: c for c in preflight(_rom_doc(600.0), "aws", workers=128,
                                          sts_client=FakeSTS(),
                                          lambda_client=FakeLambda(timeout=900, reserved=64))}
    assert slow["timeout"].level == "fail"                 # 600 s x 2.25 on Lambda > 900 s
    assert slow["concurrency"].level == "warn"             # 128 in flight against 64 reserved


def test_aws_preflight_without_a_function_fails(monkeypatch) -> None:
    monkeypatch.delenv("STRATUM_LAMBDA_FUNCTION", raising=False)
    checks = preflight(_rom_doc(), "aws", sts_client=FakeSTS(), lambda_client=FakeLambda())
    assert checks[-1].name == "worker function" and checks[-1].level == "fail"


def test_run_refuses_to_dispatch_when_preflight_fails(root: Path, monkeypatch) -> None:
    monkeypatch.setattr("stratum.executors.preflight.shutil.disk_usage",
                        lambda p: Usage(10, 10, 10))
    r = CliRunner().invoke(main, ["run", "-m", str(write_manifest(root / "m.yaml")), "--plain"])
    assert r.exit_code != 0 and "preflight failed; nothing was dispatched" in r.output
    assert "FAIL disk: storage root" in r.output
