"""The storage root when it is a bucket (06 section 4, 08 section 1).

The claim these tests defend is the one the whole design rests on: `{root}/cache/...` is "a
directory or an `s3://` prefix; layout and keys are identical". Identical means a run over a
bucket produces the same bytes as a run over a directory, and that is what
`test_an_s3_run_matches_a_local_run_byte_for_byte` checks.

Everything here uses `fake_s3`, so no test touches the network or needs credentials.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fake_s3 import install
from synthetic_nc import write_manifest, write_scenes

from stratum.cache import CacheRoot, glt_inputs
from stratum.executors import run_all
from stratum.plan import load_run, plan_run
from stratum.storage import Workspace, parse_s3
from stratum.types import GridDef, TileRef

GRID = GridDef(crs="EPSG:4326", resolution=(0.01, -0.01), origin=(-180.0, 90.0), tile_size=1.0,
               block_size=64)
TILE = TileRef(GRID, 3, -2)


def workspace(monkeypatch, tmp_path: Path, uri: str = "s3://stratum-test/") -> Workspace:
    install(monkeypatch, tmp_path / "bucket")
    return Workspace.for_root(uri, mirror=tmp_path / "mirror")


# ------------------------------------------------------------------------------------- naming
def test_parse_s3_and_local_roots() -> None:
    assert parse_s3("s3://bucket/a/b") == ("bucket", "a/b")
    assert parse_s3("s3://bucket") == ("bucket", "")
    assert parse_s3("/tmp/out") is None
    assert parse_s3("./out") is None
    with pytest.raises(ValueError, match="names no bucket"):
        parse_s3("s3:///nothing")


def test_a_local_root_is_a_workspace_with_no_remote(tmp_path: Path) -> None:
    ws = Workspace.for_root("out", base_dir=tmp_path)
    assert not ws.remote
    assert ws.path == (tmp_path / "out")
    # a local workspace's durable name IS the path, and every transfer is a no-op
    assert ws.url(tmp_path / "out" / "cache") == str(tmp_path / "out" / "cache")
    assert ws.pull_tree(tmp_path / "out") == 0


def test_durable_names_survive_a_different_mirror(monkeypatch, tmp_path: Path) -> None:
    """`url` and `local` are inverses, and `url` may not mention the mirror: `plan.json` is read
    on machines whose scratch directory is somewhere else entirely."""
    ws = workspace(monkeypatch, tmp_path, "s3://stratum-test/deep/prefix")
    url = ws.url(ws.path / "runs" / "r-1" / "plan.json")
    assert url == "s3://stratum-test/deep/prefix/runs/r-1/plan.json"
    assert str(ws.path) not in url

    elsewhere = Workspace.for_root("s3://stratum-test/deep/prefix/", mirror=tmp_path / "other")
    assert elsewhere.local(url) == tmp_path / "other" / "runs" / "r-1" / "plan.json"
    with pytest.raises(ValueError, match="not under this workspace"):
        elsewhere.local("s3://other-bucket/runs/r-1/plan.json")


def test_two_roots_never_share_a_mirror(monkeypatch, tmp_path: Path) -> None:
    install(monkeypatch, tmp_path / "bucket")
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    a = Workspace.for_root("s3://one/")
    b = Workspace.for_root("s3://two/")
    same = Workspace.for_root("s3://one/")
    assert a.path != b.path
    assert a.path == same.path, "the mirror is derived from the URI, so every process agrees"


# -------------------------------------------------------------------------------- the artifacts
def key_for(cache: CacheRoot, name: str = "G1"):
    return cache.key("glt", GRID.id, TILE, glt_inputs(name, GRID, 60.0, "kdtree", 1))


def test_a_committed_artifact_uploads_the_sidecar_last(monkeypatch, tmp_path: Path) -> None:
    """06 section 3 rule 6, over the network. The sidecar is the commit, so it must be the last
    object written - otherwise a concurrent reader sees a hit for members that are not there."""
    fake = install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "mirror")
    cache = CacheRoot(ws)

    file_key = key_for(cache)
    cache.write_file(file_key, lambda p: p.write_bytes(b"glt bytes"))
    assert fake.puts[-1].endswith(".inputs.json")

    dir_key = cache.key("product", GRID.id, TILE, {"artifact_type": "product", "n": 1})

    def members(d: Path) -> None:
        (d / "a.tif").write_bytes(b"a")
        (d / "b.tif").write_bytes(b"b")

    fake.puts.clear()
    cache.write_dir(dir_key, members)
    assert fake.puts[-1].endswith(".inputs.json")
    assert sum(1 for k in fake.puts if k.endswith(".tif")) == 2


def test_a_probe_falls_back_to_the_bucket(monkeypatch, tmp_path: Path) -> None:
    """The property that makes a worker with an empty mirror cheap: whoever computed an artifact,
    everyone else gets a hit. Two workspaces over one bucket stand in for two machines."""
    install(monkeypatch, tmp_path / "bucket")
    first = CacheRoot(Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "m1"))
    second = CacheRoot(Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "m2"))

    key = key_for(first)
    assert not second.hit(second.key("glt", GRID.id, TILE, key.inputs))
    first.write_file(key, lambda p: p.write_bytes(b"glt bytes"))

    other = second.key("glt", GRID.id, TILE, key.inputs)
    assert other.hash == key.hash, "the key is a function of the inputs, not of the machine"
    assert second.hit(other)
    assert other.path.read_bytes() == b"glt bytes"
    assert json.loads(other.inputs_path.read_text()) == dict(key.inputs)


def test_a_sidecar_with_no_members_is_not_a_hit(monkeypatch, tmp_path: Path) -> None:
    """The one corruption content addressing cannot detect is a partial artifact at a key. A
    directory whose sidecar arrived but whose members did not must read as a miss and recompute."""
    fake = install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "m1")
    cache = CacheRoot(ws)
    key = cache.key("product", GRID.id, TILE, {"artifact_type": "product", "n": 2})
    cache.write_dir(key, lambda d: (d / "a.tif").write_bytes(b"a"))

    # tear the remote copy: drop the member, keep the sidecar
    bucket = tmp_path / "bucket" / "stratum-test"
    for member in bucket.rglob("a.tif"):
        member.unlink()
    fresh = CacheRoot(Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "m2"))
    torn = fresh.key("product", GRID.id, TILE, dict(key.inputs))
    assert fake.head_object(Bucket="stratum-test", Key=ws.key(key.inputs_path))
    assert not fresh.hit(torn)
    assert not torn.path.exists(), "a torn mirror entry is cleared, not left to be read"


# ------------------------------------------------------------------------------ the whole run
def pixels(root: Path) -> dict[str, str]:
    """Every published raster, by name, digested over its PIXELS rather than its file bytes.

    Not the file bytes: a GeoTIFF carries `run_id` and `manifest_hash` as tags, and the two runs
    compared here have different manifests (one says `./out`, the other says `s3://...`), so
    their headers differ by construction. What must not differ is a single pixel."""
    import rasterio
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*.tif")):
        with rasterio.open(p) as src:
            out[str(p.relative_to(root))] = hashlib.sha256(src.read().tobytes()).hexdigest()
    return out


def names(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def local_products(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, str], set[str]]:
    """The reference: the same three synthetic granules run against a directory root."""
    root = tmp_path_factory.mktemp("local")
    write_scenes(root / "granules")
    result = plan_run(write_manifest(root / "manifest.yaml"))
    run_all(result.run_dir, workers=1)
    products = Path(result.document["products_dir"])
    return pixels(products), names(products)


def test_an_s3_run_matches_a_local_run(monkeypatch, tmp_path: Path,
                                       local_products: tuple[dict[str, str], set[str]]) -> None:
    """The acceptance criterion for the storage root: plan and run with `outputs.bucket` on S3,
    and the products are the ones a local run produced - same files, same pixels. `workers=1`
    because the fake client lives in this process and a spawned pool would not see it."""
    fake = install(monkeypatch, tmp_path / "bucket")
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    write_scenes(tmp_path / "granules")
    manifest = write_manifest(tmp_path / "manifest.yaml", bucket="s3://stratum-test/")
    result = plan_run(manifest)

    # nothing durable may name the mirror
    assert result.document["root"] == "s3://stratum-test/"
    assert result.document["products_dir"].startswith("s3://stratum-test/products/")
    assert "scratch" not in json.dumps(result.document["root"])

    run_all(result.run_dir, workers=1)

    # read the products back out of the BUCKET, not out of the mirror
    products = (tmp_path / "bucket" / "stratum-test"
                / result.document["products_dir"].removeprefix("s3://stratum-test/"))
    ref_pixels, ref_names = local_products
    assert names(products) == ref_names
    assert pixels(products) == ref_pixels

    # and the run's own record is in the bucket, not only in the mirror
    uploaded = set(fake.objects)
    run_id = result.run_id
    for name in ("plan.json", "manifest.merged.yaml", "provenance.json", "report.md"):
        assert f"stratum-test/runs/{run_id}/{name}" in uploaded, name
    assert any(k.startswith("stratum-test/cache/glt/") for k in uploaded)


def test_a_worker_rebuilds_the_run_from_the_bucket(monkeypatch, tmp_path: Path) -> None:
    """What a Lambda does at cold start: an empty mirror, the run directory pulled from the
    bucket, `load_run` and then every cache probe a hit. No recompute, no local state."""
    install(monkeypatch, tmp_path / "bucket")
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    write_scenes(tmp_path / "granules")
    result = plan_run(write_manifest(tmp_path / "manifest.yaml", bucket="s3://stratum-test/"))
    run_all(result.run_dir, workers=1)

    # a different machine: a scratch directory with nothing in it
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "cold-scratch"))
    fresh = Workspace.for_root("s3://stratum-test/")
    assert not (fresh.path / "runs").exists()
    pulled = fresh.pull_tree(fresh.path / "runs" / result.run_id)
    assert pulled, "the run directory is in the bucket"
    run = load_run(fresh.path / "runs" / result.run_id)
    assert run.run_id == result.run_id
    assert run.root == fresh.path

    from stratum.executors.worker import clear_cache, exec_item
    clear_cache()
    out = exec_item(fresh.path / "runs" / result.run_id, "regrid", 0)
    assert out["ok"] and out["hit"], "a GLT another machine wrote is a hit here"
    clear_cache()


# ------------------------------------------------------------------- reclaiming scratch space
# The asset cache, the storage mirror and the runtime share ONE filesystem - a 10 GB /tmp on
# Lambda - and everything either of the first two holds is a cache of something durable. These
# cover the guard that follows from that, and the two failures that produced it: 6,076 regrid
# items on ENOSPC with an unbounded asset cache, then 52,363 resolve items on ENOSPC with the
# asset cache capped at 5.5 GB and the mirror taking the rest.
def _aged(path: Path, size: int, age: float) -> Path:
    import os
    import time
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def _free(monkeypatch, free: int) -> None:
    """Pin reported free space, so eviction is exercised without filling a real disk."""
    import shutil as _shutil

    from stratum import storage as _storage
    real = _shutil.disk_usage

    def fake(path):
        u = real("/")
        return type(u)(total=u.total, used=u.total - free, free=free)

    monkeypatch.setattr(_storage.shutil, "disk_usage", fake)


def test_plenty_of_free_space_evicts_nothing_and_never_walks_the_tree(monkeypatch, tmp_path):
    """The common case, and the reason the probe comes before the walk: a workstation must not
    pay an rglob over an 85 GB mirror on every cache write."""
    from stratum import storage as _storage
    from stratum.storage import ensure_free

    cache = tmp_path / "assets"
    _aged(cache / "old.nc", 1000, age=10_000)
    _free(monkeypatch, 500 * 1024 * 1024 * 1024)
    walked = []
    monkeypatch.setattr(_storage, "_evict_candidates",
                        lambda roots: walked.append(roots) or [])
    assert ensure_free(0, roots=[cache], reserve=1024) == 0
    assert walked == [], "the tree must not be walked when the volume is not tight"
    assert (cache / "old.nc").exists()


def test_eviction_spans_the_asset_cache_and_the_mirror_in_one_lru(monkeypatch, tmp_path):
    """The fix for the resolve failure. A budget per directory cannot do this: staged granules
    and mirrored artifacts compete for the same bytes, so they must compete in the same LRU -
    and the biggest, coldest thing goes first whichever directory it is in."""
    from stratum.storage import ensure_free

    assets, mirror = tmp_path / "assets", tmp_path / "mirror"
    cold_asset = _aged(assets / "ab" / "cold_OBS.nc", 400, age=9_000)
    cold_snap = _aged(mirror / "cache" / "snapshot" / "t" / "cold.tif", 400, age=8_000)
    hot_glt = _aged(mirror / "cache" / "glt" / "t" / "hot.tif", 400, age=1_000)
    _free(monkeypatch, 100)
    freed = ensure_free(0, roots=[assets, mirror], reserve=900, slab=0, grace=0.0)
    assert freed == 800
    assert not cold_asset.exists() and not cold_snap.exists()
    assert hot_glt.exists(), "the most recently READ artifact must survive"


def test_a_run_directory_is_never_evicted(monkeypatch, tmp_path):
    """Work lists and plan.json are what the item is reading. Losing them fails the item
    instead of costing a re-fetch, which is the one case where eviction is not a bandwidth
    trade - so `runs/` is excluded by path, not by age."""
    from stratum.storage import ensure_free

    mirror = tmp_path / "mirror"
    work = _aged(mirror / "runs" / "r1" / "work" / "resolve.jsonl", 10_000, age=99_999)
    art = _aged(mirror / "cache" / "glt" / "t" / "a.tif", 100, age=99_999)
    _free(monkeypatch, 0)
    ensure_free(0, roots=[mirror], reserve=10_000, grace=0.0)
    assert work.exists(), "a run directory must survive even when nothing else can be freed"
    assert not art.exists()


def test_an_in_flight_write_is_never_evicted(monkeypatch, tmp_path):
    """`.part-*` is a download in progress and `.{hash}-{token}.tmp` an uncommitted cache
    write. Deleting either corrupts a write instead of reclaiming a cached byte."""
    from stratum.storage import ensure_free

    d = tmp_path / "mirror"
    part = _aged(d / "assets" / "x.nc.part-123-456", 5_000, age=99_999)
    tmp = _aged(d / "cache" / "glt" / "t" / ".abc123-dead.tmp", 5_000, age=99_999)
    done = _aged(d / "cache" / "glt" / "t" / "done.tif", 100, age=99_999)
    _free(monkeypatch, 0)
    ensure_free(0, roots=[d], reserve=10_000, grace=0.0)
    assert part.exists() and tmp.exists()
    assert not done.exists()


def test_eviction_stops_rather_than_thrashing_a_hot_scratch(monkeypatch, tmp_path):
    """Everything inside the grace window is left alone, and the caller is allowed to fail on
    ENOSPC. Pulling a file out from under a reader is worse than the error."""
    from stratum.storage import ensure_free

    d = tmp_path / "mirror"
    fresh = _aged(d / "cache" / "glt" / "t" / "fresh.tif", 5_000, age=1.0)
    _free(monkeypatch, 0)
    assert ensure_free(0, roots=[d], reserve=1_000_000, grace=60.0) == 0
    assert fresh.exists()


def test_reserve_zero_disables_eviction(monkeypatch, tmp_path):
    from stratum.storage import ensure_free

    d = tmp_path / "mirror"
    keep = _aged(d / "cache" / "glt" / "t" / "a.tif", 5_000, age=99_999)
    _free(monkeypatch, 0)
    assert ensure_free(0, roots=[d], reserve=0, grace=0.0) == 0
    assert keep.exists()


def test_a_remote_root_registers_its_mirror_and_a_local_root_does_not(monkeypatch, tmp_path):
    """The asymmetry that keeps this safe: a bucket's mirror is disposable, a local root is the
    record. Registering a local root would make eviction delete the product."""
    from stratum.storage import Workspace, evictable_roots

    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    local = Workspace.for_root(tmp_path / "out")
    assert local.path not in evictable_roots()
    remote = Workspace.for_root("s3://bucket/prefix", client=object())
    assert remote.path in evictable_roots()


def test_one_walk_buys_many_writes(monkeypatch, tmp_path):
    """Hysteresis. Eviction triggers at the reserve but frees past it, so a stage writing 20 MB
    an item does not rglob the whole mirror on every write. Without the slab this frees exactly
    one file and is back under the low-water mark immediately."""
    from stratum.storage import ensure_free

    d = tmp_path / "mirror"
    for i in range(6):
        _aged(d / "cache" / "snapshot" / "t" / f"s{i}.tif", 100, age=9_000 - i)
    _free(monkeypatch, 100)
    # low-water 200 would be met by freeing 100; the slab takes it to 500
    freed = ensure_free(0, roots=[d], reserve=200, slab=300, grace=0.0)
    assert freed == 400, "must free to the high-water mark, not the trigger"
