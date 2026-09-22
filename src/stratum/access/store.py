"""AssetStore (12 section 4): a URI becomes an openable handle.

Two modes exist. `file://` URIs and bare paths are opened in place, stage-in as identity.
`https://` assets are staged on first touch into the **node-local asset cache** (12 section 4,
"The asset cache is not the artifact cache"): streamed through the Earthdata session to a temp
file beside the target, checksum-verified when the index supplied one, then renamed into place
so a reader only ever sees a complete file and concurrent workers need no lock (12 section 8,
question 3). A reader only ever sees an `AssetHandle`, so `s3://` stage-in, `/vsis3/` streaming
and prepared assets slot in behind the same handle later; those raise NotImplementedError here,
naming the section, rather than pretending.

Credentials never appear here: the session comes from `stratum.access.auth`, imported lazily on
the first https open so a local run never imports earthaccess, and no token, password or header
is ever logged, stored or put in an exception (08 section 5). The Earthdata session - which
carries a bearer token on every request - is used only for hosts under `TRUSTED_HOSTS`
(Earthdata's own domains); any other https host is fetched with an anonymous session, and
`http://` is refused outright, so an index row naming a foreign or plaintext URL can never
leak the credential (12 section 4).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from stratum.storage import ensure_free, register_evictable
from stratum.types import AssetHandle, Credentials

REMOTE_SCHEMES = ("s3", "https")
#: Domain suffixes the Earthdata Login session may be sent to (12 section 4 "Credentials"):
#: the credential system's own hosts, never an instrument or an archive name.
TRUSTED_HOSTS: tuple[str, ...] = ("earthdata.nasa.gov", "earthdatacloud.nasa.gov")

#: Environment variable naming the node-local asset cache when the caller passes none.
ASSET_CACHE_ENV = "STRATUM_ASSET_CACHE"
#: Streaming chunk size: 1 MiB, inside the 64 KiB - 1 MiB range the slice fixed.
CHUNK_SIZE = 1 << 20
#: (connect, read) timeout in seconds for one asset download.
DEFAULT_TIMEOUT: tuple[float, float] = (30.0, 300.0)
#: HTTP statuses that mean "the credential is no longer good": re-login once, retry once.
_AUTH_STATUSES = (401, 403)
#: Transient statuses worth a bounded retry with backoff (08 section 4): rate limit, gateway.
RETRY_STATUSES = (429, 502, 503, 504)
#: An OPTIONAL policy cap on this one directory: "do not hoard more than this many bytes of
#: staged granules". Off unless set, because it is not what keeps the volume from filling -
#: `stratum.storage.ensure_free` is, and it looks at free space across everything sharing the
#: filesystem. Sizing a per-directory budget instead is what failed twice: the asset cache and
#: the storage mirror share one 10 GB `/tmp` on Lambda, so a 5.5 GB asset budget left the mirror
#: 4.5 GB, and resolve - which writes a ~20 MB snapshot per item into it and pulls every GLT,
#: aux warp and ortho warp it reads - filled that and died on ENOSPC (12 section 4).
#:
#: Worth setting on a shared workstation, where free space is plentiful but a run staging the
#: whole archive slice is still antisocial: one `plan` over the western states parked 84 GB of
#: granules in `{root}/assets` because nothing capped it and nothing needed to.
SCRATCH_BUDGET_ENV = "STRATUM_ASSET_CACHE_BUDGET_BYTES"
#: Reserved before a stage-in whose size is not known yet. EMIT's largest indexed asset is the
#: 109 MB L1B OBS; this covers one of those plus a companion and headroom.
DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024
#: An entry younger than this is never evicted, so a file another thread just opened is not
#: pulled out from under it. `prefetch` is the only multi-threaded reader and it only writes.
EVICT_GRACE_SECONDS = 60.0

#: Retries after the first attempt, and the base of the exponential backoff in seconds.
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.0
#: Threads used by `prefetch` (the planner's parallel stage-in, 12 section 7).
PREFETCH_WORKERS = 8
#: Hex-digest lengths that identify a bare (unprefixed) checksum.
_BARE_HEX_ALGOS = {128: "sha512", 64: "sha256"}


def cache_budget(cache: Path) -> int:
    """Bytes the asset cache may occupy: `$STRATUM_ASSET_CACHE_BUDGET_BYTES`, else `0`.

    `0` means this cap is off and the cache is bounded only by free space, via
    `stratum.storage.ensure_free`. The default used to be a FRACTION OF THE VOLUME, which was
    wrong in a way worth recording: it reserved most of a shared `/tmp` for one of the several
    directories on it, so the others hit ENOSPC while the budget reported room to spare.
    """
    override = os.environ.get(SCRATCH_BUDGET_ENV)
    if override is not None and override.strip():
        return max(0, int(override))
    _ = cache
    return 0


def _entries(cache: Path) -> list[tuple[float, int, Path]]:
    """`(atime, size, path)` for every complete file in the cache, oldest access first.

    `.part-*` files are in-flight downloads and are never candidates. Access time is what makes
    this an LRU rather than a FIFO: a granule read by four regrid items - one per tile it
    touches - keeps being refreshed and stays resident, while one the run has moved past ages
    out. `open()` touches a hit for exactly this reason.
    """
    out: list[tuple[float, int, Path]] = []
    for path in cache.rglob("*"):
        if ".part-" in path.name:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        out.append((st.st_atime, st.st_size, path))
    out.sort(key=lambda e: e[0])
    return out


def ensure_room(cache: Path, need: int = DEFAULT_RESERVE_BYTES, *, budget: int | None = None,
                grace: float = EVICT_GRACE_SECONDS) -> int:
    """Evict least-recently-used assets until `need` bytes fit inside the budget. Returns the
    bytes freed.

    A verbatim copy of an upstream granule is the one artifact it is always safe to discard:
    re-staging it costs a download and nothing else, whereas everything derived from it - the
    GLT, the masked observation, the ortho warp - is content-addressed in the storage root and
    survives ([06 section 2](../../docs/specs/06-caching.md)). Evicting is therefore a bandwidth
    trade, never a correctness one.

    Budget `0` disables eviction. An entry younger than `grace` is skipped rather than evicted,
    and eviction stops when nothing older remains - so a cache genuinely full of hot files is
    left alone and the caller is allowed to fail on ENOSPC rather than thrash.
    """
    budget = cache_budget(cache) if budget is None else budget
    if budget <= 0 or not cache.is_dir():
        return 0
    entries = _entries(cache)
    used = sum(size for _, size, _ in entries)
    freed = 0
    now = time.time()
    for atime, size, path in entries:
        if used + need <= budget:
            break
        if now - atime < grace:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        used -= size
        freed += size
    return freed


class AssetStoreError(RuntimeError):
    """Base class for asset staging failures. Messages carry the URL and never a credential."""


class AssetCacheUnconfigured(AssetStoreError):
    """A remote asset was opened but no node-local asset cache is configured (12 section 4)."""


class ChecksumMismatch(AssetStoreError):
    """The downloaded bytes do not match the catalogue checksum (12 section 4). The partial
    file has been deleted; nothing is left in the cache."""


class AssetFetchError(AssetStoreError):
    """An HTTP download failed. `status` is the final status code, or None when no response
    arrived (a timeout or a dropped connection; `detail` then names the exception type)."""

    def __init__(self, uri: str, status: int | None, detail: str = "") -> None:
        self.uri, self.status = uri, status
        where = f"HTTP {status}" if status is not None else "no response"
        super().__init__(f"fetching {uri!r} failed ({where}){': ' + detail if detail else ''}")

    @property
    def transient(self) -> bool:
        """Worth another attempt: no response at all, or a `RETRY_STATUSES` code."""
        return self.status is None or self.status in RETRY_STATUSES


class UntrustedScheme(AssetStoreError):
    """`http://` is refused: the Earthdata session would send its credential in plaintext, and
    a real archive asset is never served over it (12 section 4)."""


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


def _unsupported(uri: str) -> Exception:
    scheme = scheme_of(uri)
    if scheme == "s3":
        return NotImplementedError(
            f"s3:// assets are not supported in this slice ({uri!r}); direct S3 access is "
            "in-region only and comes later - use the https:// 'GET DATA' URL (12 section 4)")
    if scheme == "http":
        return UntrustedScheme(
            f"http:// assets are refused ({uri!r}): a plaintext URL would carry the Earthdata "
            "credential in the clear; use the https:// 'GET DATA' URL (12 section 4)")
    return NotImplementedError(
        f"{scheme}:// assets are not supported ({uri!r}); the schemes are file://, https:// "
        "and, later, s3:// (12 section 4)")


def host_of(uri: str) -> str:
    return (urlparse(uri).hostname or "").lower()


def is_trusted_host(uri: str, trusted: Sequence[str] = TRUSTED_HOSTS) -> bool:
    """May the Earthdata Login session - bearer token and all - be sent to this URI's host?
    True for a host equal to, or under, one of `trusted` (12 section 4)."""
    host = host_of(uri)
    return any(host == t or host.endswith("." + t) for t in (x.lower() for x in trusted))


def parse_checksum(checksum: str | None) -> tuple[str, str] | None:
    """`'sha512:<hex>'` (any hashlib algorithm name before the colon) or a bare hex digest whose
    length identifies it (128 -> sha512, 64 -> sha256) -> `(algorithm, lowercase hex)`; None for
    None. Anything else is a ValueError: a malformed checksum must not silently disable
    verification."""
    if checksum is None:
        return None
    text = str(checksum).strip()
    if not text:
        return None
    if ":" in text:
        algo, _, hexdigest = text.partition(":")
        algo = algo.strip().lower()
    else:
        hexdigest = text
        algo = _BARE_HEX_ALGOS.get(len(hexdigest), "")
        if not algo:
            raise ValueError(f"bare checksum {text[:20]!r}... has no recognised digest length")
    hexdigest = hexdigest.strip().lower()
    if algo not in hashlib.algorithms_available:
        raise ValueError(f"unknown checksum algorithm {algo!r} in {text[:24]!r}")
    if not hexdigest or any(c not in "0123456789abcdef" for c in hexdigest):
        raise ValueError(f"checksum {text[:24]!r} is not a hex digest")
    return (algo, hexdigest)


def uri_filename(uri: str) -> str:
    """The last path component of a URI, percent-decoded; `'asset'` when there is none."""
    name = unquote(urlparse(uri).path.rsplit("/", 1)[-1])
    return name or "asset"


def asset_cache_for(root: Path | str | None) -> Path | None:
    """The node-local asset cache a run uses (12 section 4): `$STRATUM_ASSET_CACHE` when set,
    else `{root}/assets` under the storage root, else None. One rule for the planner, the local
    executor and every spawned worker, so they all stage into - and hit - the same directory.
    The cache location never enters a cache key; it is where bytes land, not what they are."""
    env = os.environ.get(ASSET_CACHE_ENV)
    if env:
        return Path(env)
    return Path(root) / "assets" if root is not None else None


def asset_cache_path(asset_cache: Path, uri: str, checksum: str | None,
                     etag: str | None = None) -> Path:
    """Where one remote asset lives in the node-local cache (12 section 4).

    With a checksum: `{asset_cache}/{hex[:2]}/{hex[:16]}_{filename}`, so two indexes naming the
    same bytes share one copy and a re-delivered granule lands beside the old one. Without one:
    `{asset_cache}/uri/{sha256(uri + etag)[:16]}_{filename}` - the URI plus the ETag when the
    catalogue gave one, so a re-delivery under the same URL with a new ETag is a miss rather
    than a permanent stale hit; the URI alone when that is all the identity there is."""
    parsed = parse_checksum(checksum)
    name = uri_filename(uri)
    if parsed is not None:
        _, hexdigest = parsed
        return Path(asset_cache) / hexdigest[:2] / f"{hexdigest[:16]}_{name}"
    ident = uri if not etag else f"{uri}\n{etag}"
    key = hashlib.sha256(ident.encode()).hexdigest()[:16]
    return Path(asset_cache) / "uri" / f"{key}_{name}"


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


@dataclass(frozen=True)
class CachedAsset(LocalAsset):
    """An AssetHandle over a remote asset staged into the node-local asset cache. `uri` stays
    the remote URI the index named; `path()` is the complete local copy; `checksum` is the
    catalogue digest it was verified against, None when the index had none (12 section 4)."""

    checksum: str | None = None


class _EarthdataAuth:
    """The default `auth` callable: `earthdata_login()` on the first call, imported lazily so a
    local run never loads earthaccess; every later call is a re-login (`force=True`), which is
    what the store asks for after a 401/403 (08 section 5)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        from stratum.access.auth import earthdata_login

        self.calls += 1
        return earthdata_login(force=self.calls > 1)


def _default_session_factory(auth: Any) -> Any:
    """`auth` is the Earthdata auth object for a trusted host, or None for an anonymous
    session (a host outside `TRUSTED_HOSTS` never sees the credential)."""
    if auth is None:
        import requests

        return requests.Session()
    from stratum.access.auth import earthdata_session

    return earthdata_session(auth)


def _is_request_exception(exc: BaseException) -> bool:
    """A `requests` transport failure (timeout, reset, chunked-encoding), without importing
    requests on the local path: matched by module name."""
    return type(exc).__module__.split(".")[0] in ("requests", "urllib3")


def _netrc_has_earthdata() -> bool:
    """Offline check for an Earthdata Login entry in `$NETRC` or `~/.netrc`."""
    import netrc

    candidate = os.environ.get("NETRC") or os.path.expanduser("~/.netrc")
    try:
        entries = netrc.netrc(candidate)
    except (FileNotFoundError, OSError, netrc.NetrcParseError):
        return False
    return any(host.endswith("urs.earthdata.nasa.gov") for host in entries.hosts)


class AssetStore:
    """Every worker's door to bytes (12 section 4).

    `asset_cache` is the node-local directory remote assets are staged into: the argument, else
    `$STRATUM_ASSET_CACHE`, else none - and then opening an https asset raises
    `AssetCacheUnconfigured` rather than inventing a per-process temp dir that would defeat the
    cache (the executor passes `{root}/assets`). `auth` is a zero-argument callable returning
    the Earthdata auth object and `session_factory` turns it into a requests-like session -
    called with `None` for the anonymous session a host outside `trusted_hosts` gets; both
    default to `stratum.access.auth` / `requests` and exist so tests inject a fake session.
    `retries`/`backoff` bound the retry on transient failures (429/502/503/504, timeouts,
    resets; 08 section 4). Construction does nothing - no login, no import of earthaccess - so
    a store can be built in a spawned worker.
    """

    def __init__(
        self,
        asset_cache: Path | str | None = None,
        auth: Callable[[], Any] | None = None,
        session_factory: Callable[[Any], Any] | None = None,
        *,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        chunk_size: int = CHUNK_SIZE,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        reserve: int = DEFAULT_RESERVE_BYTES,
        trusted_hosts: Sequence[str] = TRUSTED_HOSTS,
    ) -> None:
        if asset_cache is None:
            env = os.environ.get(ASSET_CACHE_ENV)
            asset_cache = env if env else None
        self.asset_cache = Path(asset_cache) if asset_cache is not None else None
        # A staged asset is a verbatim copy of an upstream granule, so it is always safe to
        # reclaim: re-staging costs a download and nothing derived from it is lost (06 section 2).
        register_evictable(self.asset_cache)
        self._auth = auth if auth is not None else _EarthdataAuth()
        self._session_factory = (session_factory if session_factory is not None
                                 else _default_session_factory)
        self.timeout = timeout
        self.chunk_size = int(chunk_size)
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self.reserve = max(0, int(reserve))
        #: Bytes eviction reclaimed, reported so a run can say whether it was cache-bound.
        self.evicted_bytes = 0
        self.trusted_hosts = tuple(trusted_hosts)
        self._auth_obj: Any = None
        self._session: Any = None
        self._anonymous: Any = None
        self._lock = threading.Lock()
        #: Number of times the auth callable was invoked; tests count re-logins with it.
        self.logins = 0

    # ------------------------------------------------------------------------------ the contract
    def open(self, uri: str, *, etag: str | None = None,
             checksum: str | None = None) -> AssetHandle:
        """A handle for one asset. Local assets must exist: a missing file is a plan-time
        error, not something to discover in a worker (12 section 7). Remote https assets are
        staged into the asset cache on first touch and verified against `checksum` when the
        index supplied one (`'sha512:<hex>'`, or bare hex). A local file is trusted as-is; the
        checksum is asset identity for cache keys, not something to re-derive per open."""
        scheme = scheme_of(uri)
        if scheme in ("", "file"):
            path = local_path(uri)
            if not path.is_file():
                raise FileNotFoundError(f"asset {uri!r} resolved to {path}, which does not exist")
            return LocalAsset(uri=uri, etag=etag, local=path)
        if scheme == "https":
            return self._stage_https(uri, etag=etag, checksum=checksum)
        raise _unsupported(uri)

    def stage(self, uri: str, *, checksum: str | None = None) -> Path:
        """This worker, this file, now. Identity for local assets; the cached copy otherwise."""
        path = self.open(uri, checksum=checksum).path()
        assert path is not None
        return path

    def prefetch(self, items: Iterable[tuple[str, str | None]],
                 workers: int = PREFETCH_WORKERS,
                 on_done: Callable[[], None] | None = None) -> int:
        """Stage several remote assets at once - `(uri, checksum)` pairs - with a thread pool,
        so the planner's per-granule class-table pass (12 section 7) is not one serial download
        per granule. Local URIs and cache hits cost nothing; the login happens once, before the
        pool starts; the first failure propagates after the pool drains. Returns the number of
        assets actually fetched.

        `on_done` is called once per INPUT item as it is settled - skipped, already cached, or
        freshly fetched - so a caller can drive a progress bar sized on the whole list. Without
        it this is a single blocking call, which on a continental run is tens of minutes of
        silence: the planner stages every contributing granule's class-table asset, and at ~46 MB
        each that is the longest step of a plan by a wide margin.
        """
        def settled() -> None:
            if on_done is not None:
                on_done()

        todo: dict[Path, tuple[str, str | None]] = {}
        for uri, checksum in items:
            if scheme_of(uri) != "https":
                settled()
                continue
            target = asset_cache_path(self._require_cache(uri), uri, checksum)
            if target.is_file():
                settled()                       # already staged; done as far as a caller cares
            else:
                todo.setdefault(target, (uri, checksum))
        if not todo:
            return 0
        if any(is_trusted_host(u, self.trusted_hosts) for u, _ in todo.values()):
            self._session_for()
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(todo)))) as pool:
            futures = [pool.submit(self._stage_https, uri, etag=None, checksum=checksum)
                       for uri, checksum in todo.values()]
            # Drained in SUBMISSION order, not completion order, so the first failure is the
            # first submitted - which is what the docstring promises. Progress is therefore
            # slightly conservative and always monotonic.
            for fut in futures:
                fut.result()
                settled()
        return len(todo)

    def credentials_for(self, uri: str) -> Credentials:
        """What kind of credential this URI needs. For https behind Earthdata Login: `netrc`
        when a `~/.netrc` entry exists, else `bearer` (environment credentials exchanged for a
        token by the login). Never the credential itself; `expires` is None because EDL tokens
        are long-lived and the store re-logins on a 401/403 anyway (08 section 5)."""
        scheme = scheme_of(uri)
        if scheme in ("", "file"):
            return Credentials(kind="none", expires=None)
        if scheme == "https":
            if not is_trusted_host(uri, self.trusted_hosts):
                return Credentials(kind="none", expires=None)     # fetched anonymously
            return Credentials(kind="netrc" if _netrc_has_earthdata() else "bearer",
                               expires=None)
        raise _unsupported(uri)

    def cache_path(self, uri: str, checksum: str | None = None, etag: str | None = None) -> Path:
        """Where `open(uri, checksum=...)` would put (or find) the asset; see
        `asset_cache_path`. Raises `AssetCacheUnconfigured` when there is no cache."""
        return asset_cache_path(self._require_cache(uri), uri, checksum, etag)

    # ------------------------------------------------------------------------------ https path
    def _require_cache(self, uri: str) -> Path:
        if self.asset_cache is None:
            raise AssetCacheUnconfigured(
                f"opening {uri!r} needs a node-local asset cache: pass "
                f"AssetStore(asset_cache=...) or set ${ASSET_CACHE_ENV} (12 section 4)")
        return self.asset_cache

    def _stage_https(self, uri: str, *, etag: str | None, checksum: str | None) -> CachedAsset:
        """Cache hit -> the file, no network. Miss -> download to `{target}.part-{pid}`, verify,
        `os.replace` into place. Two workers may race to the same target; both downloads are
        complete and identical when renamed, so the last rename wins harmlessly (12 section 8,
        question 3). A complete file already in the cache is trusted without re-hashing: it
        was verified when written, and hashing 100 MB per open would cost more than the fetch
        it saves - the decision recorded in 12 section 4."""
        expected = parse_checksum(checksum)
        cache = self._require_cache(uri)
        target = asset_cache_path(cache, uri, checksum, etag)
        if target.is_file():
            # Mark the access so eviction sees an LRU and not a FIFO: this granule is being
            # read again, which is the signal that it should outlive one the run has passed.
            with contextlib.suppress(OSError):
                os.utime(target)
            return CachedAsset(uri=uri, etag=etag, local=target, checksum=checksum)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Two independent limits, and both are needed. `ensure_room` is POLICY - do not hoard
        # more than the budget in this one directory - and defaults to off. `ensure_free` is
        # SAFETY: the asset cache, the storage mirror and the runtime share one filesystem, so
        # what matters before a download is what the VOLUME has left, not what this directory
        # holds. Sizing them against each other is what failed: a 5.5 GB asset budget on a
        # 10 GB /tmp starved the mirror, and resolve - which writes a ~20 MB snapshot per item
        # into it - filled the remaining 4.5 GB and died on ENOSPC.
        freed = ensure_room(cache, self.reserve) + ensure_free(self.reserve)
        if freed:
            self.evicted_bytes += freed
        # the temp name carries the pid AND the thread id: `prefetch` runs several downloads
        # in one process, and two must never share a partial file
        tmp = target.with_name(f"{target.name}.part-{os.getpid()}-{threading.get_ident()}")
        try:
            for attempt in range(self.retries + 1):
                try:
                    self._download(uri, tmp, expected)
                    break
                except AssetFetchError as e:
                    tmp.unlink(missing_ok=True)
                    if not e.transient or attempt >= self.retries:
                        raise
                    time.sleep(self.backoff * (2 ** attempt))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return CachedAsset(uri=uri, etag=etag, local=target, checksum=checksum)

    def _session_for(self, *, fresh: bool = False, stale: Any = None) -> Any:
        """The Earthdata session, logging in on first use. `fresh` re-invokes the auth callable
        and rebuilds the session - the one retry after a 401/403 (08 section 5). Under a lock,
        so concurrent `prefetch` threads log in once: a `fresh` request for a session another
        thread has already replaced (`stale` is not the current one) reuses the replacement."""
        with self._lock:
            if self._session is None or (fresh and (stale is None or stale is self._session)):
                self._auth_obj = self._auth()
                self.logins += 1
                self._session = self._session_factory(self._auth_obj)
            return self._session

    def _anonymous_session(self) -> Any:
        """The credential-free session for a host outside `trusted_hosts`: no login."""
        with self._lock:
            if self._anonymous is None:
                self._anonymous = self._session_factory(None)
            return self._anonymous

    def _get(self, uri: str) -> Any:
        """One streaming GET, re-logging in once on 401/403 (trusted hosts only; an anonymous
        host has nothing to refresh). Returns a response whose status is not an auth failure;
        the caller checks the rest. A transport failure is `AssetFetchError(status=None)`."""
        trusted = is_trusted_host(uri, self.trusted_hosts)
        session = self._session_for() if trusted else self._anonymous_session()
        resp = self._request(session, uri)
        if trusted and resp.status_code in _AUTH_STATUSES:
            first = resp.status_code
            resp.close()
            session = self._session_for(fresh=True, stale=session)
            resp = self._request(session, uri)
            if resp.status_code in _AUTH_STATUSES:
                resp.close()
                raise AssetFetchError(uri, resp.status_code,
                                      f"still refused after re-login (first HTTP {first}); "
                                      "check the Earthdata credentials (08 section 5)")
        return resp

    def _request(self, session: Any, uri: str) -> Any:
        try:
            return session.get(uri, stream=True, timeout=self.timeout)
        except Exception as e:  # narrowed by module below
            if _is_request_exception(e):
                raise AssetFetchError(uri, None, type(e).__name__) from None
            raise

    def _download(self, uri: str, tmp: Path, expected: tuple[str, str] | None) -> None:
        resp = self._get(uri)
        try:
            if not 200 <= resp.status_code < 300:
                raise AssetFetchError(uri, resp.status_code)
            ctype = str((getattr(resp, "headers", None) or {}).get("Content-Type", "")).lower()
            if ctype.startswith("text/html"):
                # a 200 HTML page is a login form or an error page, never an asset - most
                # likely Earthdata Login redirected a request it could not authenticate
                raise AssetFetchError(uri, resp.status_code, f"body is {ctype!r}, not an asset; "
                                      "check the Earthdata credentials (08 section 5)")
            hasher = hashlib.new(expected[0]) if expected is not None else None
            try:
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=self.chunk_size):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        if hasher is not None:
                            hasher.update(chunk)
            except Exception as e:  # narrowed by module below
                if _is_request_exception(e):
                    raise AssetFetchError(uri, None, type(e).__name__) from None
                raise
        finally:
            resp.close()
        if expected is not None and hasher is not None:
            algo, want = expected
            got = hasher.hexdigest()
            if got != want:
                raise ChecksumMismatch(
                    f"{uri!r}: {algo} of the downloaded bytes is {got[:16]}... but the catalogue "
                    f"says {want[:16]}...; the partial file was discarded (12 section 4)")
