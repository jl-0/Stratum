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
   (06 section 3, rule 6) - a crash leaves members with no sidecar, which is not a hit;
3. a run directory or a published product tree is pushed whole.

A mirror is a cache, never the record: it may be deleted between runs, and a worker that starts
with an empty one recovers everything it needs from the bucket.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRATCH_ENV = "STRATUM_SCRATCH"
MIRROR_DIR = "stratum-mirror"


class StorageError(RuntimeError):
    """A remote read or write failed. Carries the bucket and key, never a credential."""


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
    "MIRROR_DIR", "SCRATCH_ENV", "ObjectStore", "StorageError", "Workspace", "local_workspace",
    "parse_s3", "scratch_dir",
]
