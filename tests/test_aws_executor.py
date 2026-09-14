"""The `aws` executor and the Lambda worker (08 sections 1-2, 5).

The fake Lambda here does the one thing that makes this test worth having: it calls the *real*
`handler` with the real event, in a process whose mirror is empty. So the path under test is the
one a deployment takes - event in, run directory pulled from the bucket, `exec_item`, outcome
record out - with only the transport faked.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_s3 import install
from synthetic_nc import write_manifest, write_scenes

from stratum.executors import run_all
from stratum.executors.aws import (
    AwsExecutorError,
    concurrency,
    function_name,
    invoke,
    run_stage,
)
from stratum.executors.awslambda import (
    SECRET_ENV,
    HandlerError,
    handler,
    load_credentials,
    reset_credentials,
)
from stratum.executors.worker import clear_cache
from stratum.plan import plan_run


class FakeLambda:
    """`client.invoke`, backed by the real handler. Records every event it was given."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_on = fail_on or set()

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> dict[str, Any]:
        event = json.loads(Payload)
        self.events.append(event)
        if event["index"] in self.fail_on:
            body = json.dumps({"errorType": "ValueError", "errorMessage": "synthetic failure",
                               "stackTrace": ["  line 1\n"]}).encode()
            return {"FunctionError": "Unhandled", "Payload": _Body(body)}
        clear_cache()   # a cold container: no plan cached in this "process"
        return {"Payload": _Body(json.dumps(handler(event)).encode())}


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


@pytest.fixture
def s3_run(monkeypatch, tmp_path: Path):
    """A planned run whose root is a bucket, with the mirror in a scratch directory."""
    install(monkeypatch, tmp_path / "bucket")
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    write_scenes(tmp_path / "granules")
    result = plan_run(write_manifest(tmp_path / "manifest.yaml", bucket="s3://stratum-test/"))
    clear_cache()
    return result


# ------------------------------------------------------------------------------ configuration
def test_the_function_name_comes_from_the_deployment(monkeypatch) -> None:
    monkeypatch.delenv("STRATUM_LAMBDA_FUNCTION", raising=False)
    with pytest.raises(AwsExecutorError, match="no worker function"):
        function_name()
    monkeypatch.setenv("STRATUM_LAMBDA_FUNCTION", "stratum-dev-worker")
    assert function_name() == "stratum-dev-worker"
    assert function_name("explicit") == "explicit", "--function wins over the environment"


def test_concurrency_defaults(monkeypatch) -> None:
    monkeypatch.delenv("STRATUM_LAMBDA_CONCURRENCY", raising=False)
    assert concurrency() == 32
    monkeypatch.setenv("STRATUM_LAMBDA_CONCURRENCY", "4")
    assert concurrency() == 4
    assert concurrency(7) == 7, "--workers wins"


def test_a_local_root_is_refused(monkeypatch, tmp_path: Path) -> None:
    """The failure worth catching before 700 invocations: a Lambda cannot read your laptop."""
    write_scenes(tmp_path / "granules")
    result = plan_run(write_manifest(tmp_path / "manifest.yaml"))
    monkeypatch.setenv("STRATUM_LAMBDA_FUNCTION", "stratum-dev-worker")
    clear_cache()
    with pytest.raises(AwsExecutorError, match="needs a bucket storage root"):
        run_stage(result.run_dir, "regrid", client=FakeLambda())
    clear_cache()


# ----------------------------------------------------------------------------------- the event
def test_the_event_is_four_scalars(monkeypatch, s3_run) -> None:
    """A work item carries keys and never geometry: the 256 KiB child-execution cap in 08
    section 3 is a design constraint, not an implementation detail."""
    fake = FakeLambda()
    run_stage(s3_run.run_dir, "regrid", function="fn", client=fake)
    assert fake.events, "the stage had items"
    for event in fake.events:
        assert set(event) == {"root", "run_id", "stage", "index"}
        assert event["root"] == "s3://stratum-test/"
        assert event["run_id"] == s3_run.run_id
        assert len(json.dumps(event)) < 1024
    clear_cache()


def test_the_handler_refuses_a_malformed_event() -> None:
    with pytest.raises(HandlerError, match="event lacks"):
        handler({"root": "s3://b/", "stage": "regrid"})


def test_the_handler_reports_a_missing_run(monkeypatch, tmp_path: Path) -> None:
    install(monkeypatch, tmp_path / "bucket")
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    with pytest.raises(HandlerError, match="no plan.json"):
        handler({"root": "s3://stratum-test/", "run_id": "nope", "stage": "regrid", "index": 0})


# ----------------------------------------------------------------------------- the credential
def test_the_secret_becomes_environment_variables(monkeypatch, tmp_path: Path) -> None:
    """08 section 5: the value is fetched at run time and never configured into the function.
    Both documented shapes are supported and neither is preferred by the code."""
    import boto3

    secrets = {"stratum/dev/earthdata": json.dumps({"username": "u", "password": "p"})}

    class FakeSecrets:
        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
            return {"SecretString": secrets[SecretId]}

    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: FakeSecrets())
    monkeypatch.setenv(SECRET_ENV, "stratum/dev/earthdata")
    for var in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "EARTHDATA_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    reset_credentials()
    assert load_credentials() == ["EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"]
    import os
    assert os.environ["EARTHDATA_USERNAME"] == "u"
    assert load_credentials() == [], "a warm container does not refetch"

    secrets["stratum/dev/earthdata"] = json.dumps({"token": "t"})
    reset_credentials()
    assert load_credentials(force=True) == ["EARTHDATA_TOKEN"]

    secrets["stratum/dev/earthdata"] = json.dumps({"user": "u"})
    reset_credentials()
    with pytest.raises(HandlerError, match=r"unexpected key\(s\) \['user'\]"):
        load_credentials()
    reset_credentials()


def test_no_secret_configured_is_not_an_error(monkeypatch) -> None:
    """A run over public assets, and every test: an unset secret name means do nothing."""
    monkeypatch.delenv(SECRET_ENV, raising=False)
    reset_credentials()
    assert load_credentials() == []


# --------------------------------------------------------------------------------- the run
def test_a_whole_run_executes_in_lambda(monkeypatch, s3_run) -> None:
    """Every work item of every stage dispatched as an invocation, and the run finishes with
    products, a STAC collection and provenance in the bucket - the second row of the deployment
    table in the runbook."""
    fake = FakeLambda()
    monkeypatch.setattr("stratum.executors.aws._client", lambda client=None: client or fake)
    monkeypatch.setenv("STRATUM_LAMBDA_FUNCTION", "stratum-dev-worker")
    execution = run_all(s3_run.run_dir, workers=2, executor="aws")

    assert execution["executor"] == "aws"
    assert sum(s["items"] for s in execution["stages"].values()) == len(fake.events)
    assert all(s["items"] for s in execution["stages"].values())

    bucket = Path(str(s3_run.run_dir).split("scratch")[0]) / "bucket" / "stratum-test"
    products = bucket / s3_run.document["products_dir"].removeprefix("s3://stratum-test/")
    assert list(products.rglob("*.tif")), "publish ran in Lambda and pushed its tree"
    assert (products / "collection.json").is_file(), "Finalize mirrored the items back"
    assert (bucket / "runs" / s3_run.run_id / "provenance.json").is_file()
    clear_cache()


def test_a_failed_invocation_becomes_an_outcome_record(monkeypatch, s3_run) -> None:
    """A handler exception must read like a local failure: `stratum status --failed` parses one
    file, not two formats."""
    from stratum.executors.local import ExecutionError
    from stratum.plan.document import read_results

    fake = FakeLambda(fail_on={0})
    with pytest.raises(ExecutionError, match="1 of .* item\\(s\\) failed"):
        run_stage(s3_run.run_dir, "regrid", function="fn", client=fake)
    outcomes = read_results(s3_run.run_dir, "regrid")
    assert outcomes[0]["ok"] is False
    assert "ValueError: synthetic failure" in outcomes[0]["error"]
    assert all(r["ok"] for r in outcomes[1:])
    clear_cache()


def test_invoke_survives_a_non_json_payload() -> None:
    class Garbage:
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            return {"FunctionError": "Unhandled", "Payload": _Body(b"<html>504</html>")}

    out = invoke(Garbage(), "fn", {"stage": "regrid", "index": 3})
    assert out["ok"] is False and "504" in out["error"]


def test_the_image_cmd_names_a_real_handler() -> None:
    """The Dockerfile's CMD is the only place the handler is named as a string, so a rename here
    breaks the deployment and nothing else. One import is enough to catch it."""
    import importlib
    from pathlib import Path as P

    dockerfile = P(__file__).resolve().parent.parent / "Dockerfile"
    line = next(line for line in dockerfile.read_text().splitlines() if line.startswith("CMD"))
    ref = line.split('"')[1]
    module, _, attr = ref.rpartition(".")
    assert callable(getattr(importlib.import_module(module), attr)), ref
