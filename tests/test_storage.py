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
import os
import time
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


def test_a_hit_pins_the_artifact_and_its_sidecar_so_a_later_pull_cannot_evict_it(
        monkeypatch, tmp_path):
    """The reduce failure, reproduced at the level the property actually lives.

    A reduce item calls `hit()` for every epoch's snapshot and only THEN reads them, and each of
    those hits may reclaim space to make room for its own pull. Item 3368 of the western run hit
    for 8 snapshots, and by the time `read_snapshot` opened the first one its `.tif`s were gone:

        FileNotFoundError: no snapshot layers under .../cache/snapshot/<grid>/-111_37/<hash>

    S3 still held all ten layers, so nothing was lost - only the local mirror copy, by eviction.
    Asserting on `_evict_candidates` rather than on bytes freed, because "a claimed artifact is
    never a candidate" IS the property; inferring it from a byte count couples the test to file
    sizes, the reserve and the slab, and that arithmetic is not what is being tested."""
    from stratum.storage import _evict_candidates, clear_pins

    install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "mirror")
    cache = CacheRoot(ws)
    clear_pins()

    snap = cache.key("product", GRID.id, TILE, {"artifact_type": "product", "n": 1})
    cache.write_dir(snap, lambda d: (d / "mineral_1_native.tif").write_bytes(b"x" * 400))
    unclaimed = cache.key("glt", GRID.id, TILE, {"artifact_type": "glt", "n": 2})
    cache.write_file(unclaimed, lambda p: p.write_bytes(b"y" * 400))
    for f in (*snap.path.iterdir(), unclaimed.path, unclaimed.inputs_path):
        os.utime(f, (time.time() - 9_000, time.time() - 9_000))

    assert cache.hit(snap)
    names = {p.name for _, _, p in _evict_candidates([ws.path])}
    assert "mineral_1_native.tif" not in names, "a claimed member must not be a candidate"
    assert unclaimed.path.name in names, "the unclaimed artifact still is"


def test_a_hit_marks_the_artifact_used_even_though_reading_it_would_not(monkeypatch, tmp_path):
    """`relatime` - the default nearly everywhere, including a Lambda /tmp - only advances access
    time when it is already older than the modification time, so READING a cached file does not
    reliably update its atime. Without an explicit touch this LRU silently degrades into a FIFO,
    and a snapshot resolve wrote hours ago is the coldest thing in the mirror at exactly the
    moment reduce needs it."""
    from stratum.storage import _evict_candidates, clear_pins

    install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "mirror")
    cache = CacheRoot(ws)
    clear_pins()

    key = cache.key("glt", GRID.id, TILE, {"artifact_type": "glt", "n": 5})
    cache.write_file(key, lambda p: p.write_bytes(b"z" * 400))
    os.utime(key.path, (time.time() - 9_000, time.time() - 9_000))
    assert cache.hit(key)
    clear_pins()          # isolate the touch from the pin

    ages = {p.name: time.time() - a for a, _, p in _evict_candidates([ws.path])}
    assert ages[key.path.name] < 60, "a hit must advance access time, not leave it at write time"


def test_clearing_pins_at_the_item_boundary_releases_the_previous_claim(monkeypatch, tmp_path):
    """Pins last for one work item, not for the life of a warm execution environment - or a
    container that has run a few hundred items would have nothing left to evict."""
    from stratum.storage import _evict_candidates, clear_pins

    install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "mirror")
    cache = CacheRoot(ws)
    clear_pins()

    key = cache.key("glt", GRID.id, TILE, {"artifact_type": "glt", "n": 3})
    cache.write_file(key, lambda p: p.write_bytes(b"z" * 400))
    assert cache.hit(key)
    assert key.path.name not in {p.name for _, _, p in _evict_candidates([ws.path])}
    clear_pins()
    assert key.path.name in {p.name for _, _, p in _evict_candidates([ws.path])}


def test_a_local_root_is_neither_touched_nor_pinned(monkeypatch, tmp_path):
    """On a local root the path IS the artifact, so there is nothing to mirror and nothing that
    may be evicted. Claiming it would be meaningless, and registering it dangerous."""
    from stratum.storage import clear_pins, evictable_roots, is_pinned

    clear_pins()
    cache = CacheRoot(tmp_path / "root")
    key = cache.key("glt", GRID.id, TILE, {"artifact_type": "glt", "n": 4})
    cache.write_file(key, lambda p: p.write_bytes(b"local"))
    assert cache.hit(key)
    assert not is_pinned(key.path)
    assert (tmp_path / "root") not in evictable_roots()


def test_finalize_pulls_only_the_item_documents(monkeypatch, tmp_path):
    """Finalize needs each published tile's `item.json` to build the STAC collection, and
    `write_stac_collection` never opens a COG. Pulling the whole products tree to get them cost
    19 GB and about two hours of silence on the western run - after every stage had already
    succeeded - for roughly a megabyte of JSON."""
    install(monkeypatch, tmp_path / "bucket")
    ws = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "mirror")

    products = ws.path / "products" / "run-1"
    for tile in ("-119_35", "-118_35"):
        d = products / tile / "20250101_20251231"
        d.mkdir(parents=True)
        (d / "item.json").write_text('{"id": "x"}')
        (d / "mineral_1_native.tif").write_bytes(b"\0" * 5000)
        (d / "depth_1.tif").write_bytes(b"\0" * 5000)
    ws.push_tree(products)
    for f in products.rglob("*"):
        if f.is_file():
            f.unlink()

    pulled = ws.pull_tree(products, only="item.json")
    assert pulled == 2, "one document per published tile, and nothing else"
    assert sorted(p.name for p in products.rglob("*") if p.is_file()) == ["item.json"] * 2
    assert not list(products.rglob("*.tif")), "the COGs must stay in the bucket"

    assert ws.pull_tree(products) == 4, "unfiltered still fetches everything"


# ------------------------------------------------------------- what an S3 failure actually says
def _client_error(status, code, message, op="HeadObject"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, op)


def test_an_s3_error_reports_the_status_and_code_not_just_its_class():
    """It used to re-raise as the bare string `ClientError`, so an expired session and a missing
    object produced the same unreadable message and the only clue was the chained traceback."""
    from stratum.storage import _s3_detail

    detail = _s3_detail(_client_error(404, "NoSuchKey", "The specified key does not exist."))
    assert "HTTP 404" in detail and "NoSuchKey" in detail
    assert "credentials" not in detail, "a missing object is not a credentials problem"


def test_expired_credentials_say_so_and_say_what_to_do():
    from stratum.storage import _s3_detail

    detail = _s3_detail(_client_error(400, "ExpiredToken", "The provided token has expired."))
    assert "ExpiredToken" in detail
    assert "refresh your AWS session" in detail


def test_a_naked_400_from_a_head_explains_why_it_is_naked():
    """The one that bit on the western run: `HeadObject` is a HEAD, so there is no response body
    for S3 to put an error code in, and botocore reports `400 Bad Request` with nothing else.
    The same credentials on a GET-based call said `ExpiredToken` outright."""
    from stratum.storage import _s3_detail

    detail = _s3_detail(_client_error(400, "400", "Bad Request"))
    assert "HTTP 400" in detail
    assert "carries no error body" in detail and "Expired credentials" in detail


def test_a_real_error_with_a_real_code_is_not_second_guessed():
    from stratum.storage import _s3_detail

    detail = _s3_detail(_client_error(400, "InvalidRange", "The requested range is not satisfiable"))
    assert "InvalidRange" in detail
    assert "carries no error body" not in detail, "do not guess when S3 has told you"


def test_a_head_that_cannot_mean_absent_is_raised_not_swallowed(monkeypatch, tmp_path):
    """The landmine: `head` is a cache probe, so "no" must mean absent. An expired session
    answering 403 read as a miss, and a miss makes the stage recompute - resolve rebuilding its
    ortho warps is 89 GiB of work, so the failure was an expensive silence rather than an error."""
    import pytest
    from botocore.exceptions import ClientError

    from stratum.storage import ObjectStore, StorageError

    class Client:
        def __init__(self, code):
            self.code = code

        def head_object(self, **kw):
            raise ClientError({"Error": {"Code": self.code, "Message": "nope"},
                               "ResponseMetadata": {"HTTPStatusCode": 400}}, "HeadObject")

    store = ObjectStore("b", client=Client("ExpiredToken"))
    with pytest.raises(StorageError, match="refresh your AWS session"):
        store.head("cache/glt/x.tif")

    assert ObjectStore("b", client=Client("NoSuchKey")).head("k") is False
    assert ObjectStore("b", client=Client("404")).head("k") is False


def test_an_ambiguous_403_still_means_absent_but_says_so_once(monkeypatch, caplog):
    """A deployment without ListBucket answers every miss with 403, so that has to keep meaning
    absent. It is warned about once per process - not once per probe, which would be a line per
    cache lookup."""
    import logging

    from botocore.exceptions import ClientError

    from stratum.storage import ObjectStore

    class Client:
        def head_object(self, **kw):
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject")

    monkeypatch.setattr(ObjectStore, "_warned_403", False)
    store = ObjectStore("b", client=Client())
    with caplog.at_level(logging.WARNING, logger="stratum.storage"):
        assert store.head("k1") is False
        assert store.head("k2") is False
    assert sum(1 for r in caplog.records if "403" in r.getMessage()) == 1
