"""AssetStore (12 section 4): a URI becomes an openable handle.

First slice: `file://` URIs and bare paths, stage-in as identity. A reader only ever sees an
`AssetHandle`, so the remote modes - `s3://` stage-in, `/vsis3/` streaming, prepared assets,
Earthdata credentials - slot in behind the same handle later (12 section 4). Those raise
NotImplementedError here, naming the section, rather than pretending.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from stratum.types import AssetHandle, Credentials

REMOTE_SCHEMES = ("s3", "https", "http")


def scheme_of(uri: str) -> str:
    """'file' for file:// URIs, '' for bare paths, otherwise the URI scheme."""
    parsed = urlparse(uri)
    # A single-letter scheme is a Windows drive, not a scheme; treat as a bare path.
    if len(parsed.scheme) <= 1:
        return ""
    return parsed.scheme.lower()


def is_local(uri: str) -> bool:
    return scheme_of(uri) in ("", "file")


def local_path(uri: str) -> Path:
    """The filesystem path behind a `file://` URI or a bare path. Absolute, symlinks kept."""
    scheme = scheme_of(uri)
    if scheme == "file":
        parsed = urlparse(uri)
        return Path(url2pathname(unquote(parsed.path))).absolute()
    if scheme == "":
        return Path(uri).expanduser().absolute()
    raise ValueError(f"{uri!r} is not a local URI")


def to_uri(path: Path | str) -> str:
    """A `file://` URI for a local path - what the index stores for a LocalSource asset."""
    return Path(path).absolute().as_uri()


def _unsupported(uri: str) -> NotImplementedError:
    return NotImplementedError(
        f"{scheme_of(uri)}:// assets are not supported in this slice ({uri!r}); stage-in and "
        "streaming for s3:// and https:// are 12 section 4")


@dataclass(frozen=True)
class LocalAsset:
    """An AssetHandle over a file already on this machine. `path()` and `vsi()` are the same
    location; `etag` is None because nothing catalogued it (12 section 4)."""

    uri: str
    etag: str | None
    local: Path

    def path(self) -> Path:
        return self.local

    def vsi(self) -> str:
        return str(self.local)


class AssetStore:
    """Every worker's door to bytes (12 section 4). `scratch` is where stage-in would copy to;
    unused while every asset is local, kept so the signature does not move later."""

    def __init__(self, scratch: Path | None = None) -> None:
        self.scratch = Path(scratch) if scratch is not None else None

    def open(self, uri: str, *, etag: str | None = None) -> AssetHandle:
        """A handle for one asset. Local assets must exist: a missing file is a plan-time
        error, not something to discover in a worker (12 section 7)."""
        if not is_local(uri):
            raise _unsupported(uri)
        path = local_path(uri)
        if not path.is_file():
            raise FileNotFoundError(f"asset {uri!r} resolved to {path}, which does not exist")
        return LocalAsset(uri=uri, etag=etag, local=path)

    def stage(self, uri: str) -> Path:
        """This worker, this file, now. Identity for local assets."""
        return self.open(uri).path()

    def credentials_for(self, uri: str) -> Credentials:
        if not is_local(uri):
            raise _unsupported(uri)
        return Credentials(kind="none", expires=None)
