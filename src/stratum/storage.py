"""The storage root: a local directory, or an S3 prefix with a node-local mirror (06 section 4,
08 section 1).

`outputs.bucket` is the root, and `cache/`, `runs/{run_id}/` and `products/{run_id}/` hang off it
with identical layout and identical keys whichever it is. That sentence is the whole contract, and
this module is what makes it true for `s3://`.

**Nothing above this module handles a URI.** A `Workspace` gives every stage a real
`pathlib.Path` to work with - the local root itself when the root is local, a node-local mirror
when it is a bucket - so the read path in resolve, reduce and publish is byte-for-byte the same
code either way. GDAL and netCDF4 want a file, Lambda gives us 10 GB of `/tmp`, and a block is
about 1 GB, so mirroring is not a compromise here: it is the shape the readers already need.

The remote is touched at exactly three moments, all of them in `stratum.cache`:

1. a cache probe that misses locally falls back to the bucket before it is a miss;
2. an artifact that has just committed locally is uploaded, **members first and
   `.inputs.json` last**, which is the same ordering rule an atomic local write obeys
   (06 section 3, rule 7) - a crash leaves members with no sidecar, which is not a hit;
3. a run directory or a published product tree is pushed whole.

A mirror is a cache, never the record: it may be deleted between runs, and a worker that starts
with an empty one recovers everything it needs from the bucket.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRATCH_ENV = "STRATUM_SCRATCH"
MIRROR_DIR = "stratum-mirror"

#: Bytes to leave free on the scratch volume after any staged write. Eviction is driven by what
#: the VOLUME has left rather than by a per-directory budget, because the asset cache, the mirror
#: and the runtime all share one filesystem - on Lambda a single 10 GB `/tmp` - and any split
#: between them is a guess that starves whichever side guessed low.
EVICT_RESERVE_ENV = "STRATUM_SCRATCH_RESERVE_BYTES"
DEFAULT_EVICT_RESERVE = 1536 * 1024 * 1024
#: An entry accessed more recently than this is never evicted, so a file another thread just
#: opened is not pulled out from under it.
EVICT_GRACE_SECONDS = 60.0
#: Hysteresis. Eviction TRIGGERS at `reserve` but frees down to `reserve + slab`, so one tree
#: walk buys many writes. Without it a stage that writes 20 MB per item and sits just under the
#: reserve would rglob the whole mirror on every single write - correct, but needlessly so.
#: `None` means "a slab as big as the reserve".
DEFAULT_EVICT_SLAB: int | None = None
#: Never evicted: a run directory holds `plan.json` and the work lists every item is reading, it
#: is kilobytes per item, and losing it fails the item rather than costing a re-fetch.
EVICT_KEEP_DIRS = frozenset({"runs"})

_EVICTABLE: set[Path] = set()


class StorageError(RuntimeError):
    """A remote read or write failed. Carries the bucket and key, never a credential."""


# --------------------------------------------------------------------------------- reclaiming
def register_evictable(path: Path | str | None) -> None:
    """Declare a directory whose contents this process may delete to make room.

    Only ever a node-local cache of something durable: the asset cache (a verbatim copy of an
    upstream granule) or a mirror of the storage bucket. Both are re-fetchable by construction -
    see this module's docstring - so evicting is a bandwidth trade, never a correctness one. A
    LOCAL storage root is never registered: there the mirror IS the record.
    """
    if path is not None:
        _EVICTABLE.add(Path(path))


def evictable_roots() -> list[Path]:
    return sorted(_EVICTABLE)


def evict_reserve() -> int:
    """Bytes to keep free: `$STRATUM_SCRATCH_RESERVE_BYTES`, else `DEFAULT_EVICT_RESERVE`.
    `0` disables eviction entirely."""
    raw = os.environ.get(EVICT_RESERVE_ENV)
    if raw is not None and raw.strip():
        return max(0, int(raw))
    return DEFAULT_EVICT_RESERVE


def _is_in_flight(path: Path) -> bool:
    """A partial download (`.part-*`) or an uncommitted cache write (`.{hash}-{token}.tmp`).
    Deleting either corrupts a write in progress rather than reclaiming a cached byte."""
    return ".part-" in path.name or ".tmp" in path.name


def _evict_candidates(roots: Iterable[Path]) -> list[tuple[float, int, Path]]:
    """`(atime, size, path)` for every evictable file under `roots`, oldest access first.

    Access time, not write time, is what makes this an LRU: a tile's aux warp is read by 25
    blocks x 52 epochs and a granule's GLT by every epoch that granule appears in, so both keep
    being refreshed and stay resident, while a snapshot - written once and never re-read by the
    stage that wrote it - ages out. A FIFO would evict exactly the wrong ones.
    """
    out: list[tuple[float, int, Path]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if _is_in_flight(path) or EVICT_KEEP_DIRS & set(path.parts):
                continue
            resolved = path.absolute()
            if resolved in seen:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen.add(resolved)
            out.append((st.st_atime, st.st_size, path))
    out.sort(key=lambda e: e[0])
    return out


def ensure_free(need: int, *, roots: Sequence[Path] | None = None, reserve: int | None = None,
                slab: int | None = DEFAULT_EVICT_SLAB,
                grace: float = EVICT_GRACE_SECONDS) -> int:
    """Evict least-recently-used cached files when the volume has less than `need + reserve`
    bytes free, down to `need + reserve + slab`. Returns the bytes reclaimed.

    The free-space probe comes first and is cheap, so the expensive tree walk only happens when
    the volume is actually tight - which on a workstation is never - and the slab means one walk
    then buys many writes. Eviction stops when nothing older than `grace` remains, so a scratch
    volume genuinely full of hot files is left alone and the caller is allowed to fail on ENOSPC
    rather than thrash: pulling a file out from under a reader is worse than the error.
    """
    reserve = evict_reserve() if reserve is None else reserve
    if reserve <= 0:
        return 0
    candidates = evictable_roots() if roots is None else [Path(r) for r in roots]
    probe = next((r for r in candidates if r.is_dir()), None)
    if probe is None:
        return 0
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return 0
    if free >= need + reserve:               # low-water mark: nothing to do
        return 0
    target = need + reserve + (reserve if slab is None else slab)
    freed = 0
    now = time.time()
    for atime, size, path in _evict_candidates(candidates):
        if free + freed >= target:           # high-water mark: stop, having bought headroom
            break
        if now - atime < grace:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        freed += size
    return freed


# ------------------------------------------------------------------------------------ the URI
def parse_s3(value: str) -> tuple[str, str] | None:
    """`s3://bucket/prefix` -> `(bucket, "prefix")`; `None` for anything that is not S3. The
    prefix never has a leading or trailing slash, so joining is unambiguous."""
    parsed = urlparse(str(value))
    if parsed.scheme.lower() != "s3":
        return None
    if not parsed.netloc:
        raise ValueError(f"{value!r} names no bucket")
    return parsed.netloc, parsed.path.strip("/")


def scratch_dir() -> Path:
    """Where mirrors live: `$STRATUM_SCRATCH`, else the platform temp directory. On Lambda the
    deployment sets it to `/tmp`, which is the only writable filesystem there."""
    return Path(os.environ.get(SCRATCH_ENV) or tempfile.gettempdir())


# --------------------------------------------------------------------------------- the objects
class ObjectStore:
    """The four operations a mirror needs, over one bucket. boto3 is imported lazily so that a
    local run - and every test that never touches S3 - neither imports it nor builds a client."""

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        self.bucket = bucket
        self._client = client

    @cached_property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as e:  # pragma: no cover - a packaging failure, not a runtime one
            raise StorageError("an s3:// storage root needs boto3, which is not installed") from e
        return boto3.client("s3")

    def head(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "403"):
                return False
            raise StorageError(f"HEAD s3://{self.bucket}/{key}: {type(e).__name__}") from e
        return True

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", ())]
        return keys

    def list_dirs(self, prefix: str) -> list[str]:
        """The immediate sub-prefixes of `prefix`, each with its trailing slash.

        A delimited list: it returns the directory names without walking what is inside them,
        which is what lets a viewer learn the published runs without listing their thousands of
        objects (`stratum.preview.catalog`).
        """
        prefixes: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            prefixes += [p["Prefix"] for p in page.get("CommonPrefixes", ())]
        return prefixes

    def get(self, key: str, dest: Path) -> None:
        """Download to `dest` through a temp file, so an interrupted download cannot be read as
        a complete mirror entry."""
        from botocore.exceptions import ClientError
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
        try:
            self.client.download_file(self.bucket, key, str(tmp))
            os.replace(tmp, dest)
        except ClientError as e:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"GET s3://{self.bucket}/{key}: {type(e).__name__}") from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def put(self, key: str, src: Path) -> None:
        from botocore.exceptions import ClientError
        try:
            self.client.upload_file(str(src), self.bucket, key)
        except ClientError as e:
            raise StorageError(f"PUT s3://{self.bucket}/{key}: {type(e).__name__}") from e


# ------------------------------------------------------------------------------- the workspace
@dataclass(frozen=True)
class Workspace:
    """One storage root, as the stages see it.

    `uri` is what a manifest wrote and what `plan.json` records - the value that is the same on
    every machine. `path` is where this process reads and writes, which for a bucket is a mirror
    and is therefore machine-specific and disposable. Nothing durable may record `path`.
    """

    uri: str
    path: Path
    store: ObjectStore | None = None
    prefix: str = ""

    # -- construction ---------------------------------------------------------------------
    @classmethod
    def for_root(cls, value: str | Path, *, base_dir: Path | None = None,
                 mirror: Path | None = None, client: Any | None = None) -> Workspace:
        """Build the workspace for `outputs.bucket`. A relative local path resolves against
        `base_dir` (the manifest's directory); an `s3://` root gets a mirror under
        `scratch_dir()`, named by a hash of the URI so two roots never share one."""
        text = str(value)
        parsed = parse_s3(text)
        if parsed is None:
            path = Path(text.removeprefix("file://")).expanduser()
            if not path.is_absolute():
                path = ((base_dir or Path.cwd()) / path).resolve()
            return cls(uri=str(path), path=path)
        bucket, prefix = parsed
        uri = f"s3://{bucket}/" + (f"{prefix}/" if prefix else "")
        if mirror is None:
            mirror = scratch_dir() / MIRROR_DIR / hashlib.sha256(uri.encode()).hexdigest()[:16]
        mirror.mkdir(parents=True, exist_ok=True)
        # A remote root's mirror is disposable by definition, so it may be reclaimed under disk
        # pressure. A local root is NOT registered: there this path is the record itself.
        register_evictable(mirror)
        return cls(uri=uri, path=mirror, store=ObjectStore(bucket, client=client), prefix=prefix)

    @property
    def remote(self) -> bool:
        return self.store is not None

    # -- naming ---------------------------------------------------------------------------
    def key(self, path: Path | str) -> str:
        """The object key for a path inside the mirror."""
        rel = Path(path).resolve().relative_to(self.path.resolve()).as_posix()
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def url(self, path: Path | str) -> str:
        """The durable name of a path inside this workspace: an `s3://` URI, or the path
        itself. This is what goes into `plan.json`, STAC and provenance."""
        if not self.remote:
            return str(Path(path))
        return f"s3://{self.store.bucket}/{self.key(path)}"  # type: ignore[union-attr]

    def local(self, url: str | Path) -> Path:
        """The inverse of `url`: where a durable name lives in this process."""
        text = str(url)
        if self.remote and text.startswith(self.uri):
            return self.path / text[len(self.uri):]
        if self.remote and parse_s3(text) is not None:
            raise ValueError(f"{text} is not under this workspace's root {self.uri}")
        return Path(text)

    # -- transfer -------------------------------------------------------------------------
    def push_file(self, path: Path) -> None:
        if self.remote and path.is_file():
            self.store.put(self.key(path), path)  # type: ignore[union-attr]

    def push_tree(self, path: Path, *, last: Path | None = None) -> None:
        """Upload every file under `path`, with `last` (an artifact's `.inputs.json`) uploaded
        after all the others. The ordering is the commit: a reader treats the sidecar as the
        proof that the members are all there."""
        if not self.remote or not path.exists():
            return
        if path.is_file():
            self.push_file(path)
            return
        members = sorted(p for p in path.rglob("*") if p.is_file())
        deferred = last.resolve() if last is not None else None
        for member in members:
            if deferred is not None and member.resolve() == deferred:
                continue
            self.push_file(member)
        if deferred is not None and Path(deferred).is_file():
            self.push_file(Path(deferred))

    def exists(self, path: Path) -> bool:
        """Does the object for this mirror path exist remotely?"""
        return bool(self.remote and self.store.head(self.key(path)))  # type: ignore[union-attr]

    def pull_file(self, path: Path) -> bool:
        """Fetch one object into the mirror. True when the mirror now has it."""
        if path.is_file():
            return True
        if not self.remote:
            return False
        key = self.key(path)
        if not self.store.head(key):  # type: ignore[union-attr]
            return False
        self.store.get(key, path)  # type: ignore[union-attr]
        return True

    def pull_tree(self, path: Path) -> int:
        """Fetch every object under a prefix into the mirror. Returns how many were fetched;
        a file already present is left alone, because the mirror holds only committed bytes."""
        if not self.remote:
            return 0
        prefix = self.key(path).rstrip("/") + "/"
        fetched = 0
        for key in self.store.list(prefix):  # type: ignore[union-attr]
            dest = self.path / key[len(self.prefix) + 1:] if self.prefix else self.path / key
            if dest.is_file():
                continue
            self.store.get(key, dest)  # type: ignore[union-attr]
            fetched += 1
        return fetched

    def drop_mirror(self, path: Path) -> None:
        """Remove a mirrored path. Only ever called on a remote workspace - deleting from a
        local root would be deleting the artifact itself."""
        if not self.remote:
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def local_workspace(root: Path | str) -> Workspace:
    """A workspace over a plain directory - what `CacheRoot(path)` wraps itself in."""
    path = Path(root)
    return Workspace(uri=str(path), path=path)


__all__ = [
    "DEFAULT_EVICT_RESERVE", "DEFAULT_EVICT_SLAB", "EVICT_GRACE_SECONDS", "EVICT_RESERVE_ENV", "MIRROR_DIR",
    "SCRATCH_ENV", "ObjectStore", "StorageError", "Workspace", "ensure_free", "evict_reserve",
    "evictable_roots", "local_workspace", "parse_s3", "register_evictable", "scratch_dir",
]
