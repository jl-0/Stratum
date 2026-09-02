"""AssetStore https staging into the node-local asset cache (12 section 4).

Everything here runs offline against a fake requests-like session injected through
`session_factory`; the one live test is gated behind STRATUM_LIVE=1.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from stratum.access.store import (
    ASSET_CACHE_ENV,
    AssetCacheUnconfigured,
    AssetFetchError,
    AssetStore,
    CachedAsset,
    ChecksumMismatch,
    LocalAsset,
    UntrustedScheme,
    asset_cache_path,
    is_trusted_host,
    parse_checksum,
    to_uri,
)

URL = "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/X.001/gran/X_001_gran.nc"
FOREIGN = "https://example.invalid/bucket/X_001_gran.nc"
BODY = bytes(range(256)) * 2000 + b"tail"          # 512 004 bytes: several chunks at 64 KiB
SHA = hashlib.sha512(BODY).hexdigest()
SECRET = "Bearer not-a-real-token-but-must-never-appear"


class ReadTimeout(Exception):
    """Stands in for `requests.exceptions.ReadTimeout`: matched by module, like the real one."""


ReadTimeout.__module__ = "requests.exceptions"


# ------------------------------------------------------------------------------------ fakes
class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None,
                 delay: float = 0.0) -> None:
        self.status_code, self._body, self.closed = status, body, False
        self.headers = dict(headers or {})
        self.delay = delay

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._body), chunk_size):
            if self.delay:
                time.sleep(self.delay)
            yield self._body[i:i + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """`get()` streams `body`; `statuses` is consumed one per call (last repeats). The list is
    shared by reference so a re-login's fresh session continues the same sequence. `raises`
    is consumed first: each entry is raised by one `get()` before any response. An anonymous
    session (`auth_obj` None) carries no Authorization header, as `requests.Session()`
    would not."""

    def __init__(self, body: bytes = BODY, statuses: list[int] | None = None,
                 auth_obj=None, raises: list[Exception] | None = None,
                 headers: dict | None = None, delay: float = 0.0) -> None:
        self.body, self.statuses = body, (statuses if statuses is not None else [200])
        self.calls: list[dict] = []
        self.auth_obj = auth_obj
        self.headers = {"Authorization": SECRET} if auth_obj is not None else {}
        self.responses: list[FakeResponse] = []
        self.raises = raises if raises is not None else []
        self.response_headers, self.delay = headers, delay
        self.lock = threading.Lock()

    def get(self, url: str, *, stream: bool, timeout) -> FakeResponse:
        with self.lock:
            self.calls.append({"url": url, "stream": stream, "timeout": timeout})
            if self.raises:
                raise self.raises.pop(0)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            resp = FakeResponse(status, self.body, self.response_headers, self.delay)
            self.responses.append(resp)
        return resp


class Harness:
    """A store wired to fake auth: `auths` counts logins, `sessions` every session built.
    `backoff` is 0 so retries are instant."""

    def __init__(self, cache: Path | None, *, body: bytes = BODY,
                 statuses: tuple[int, ...] = (200,), raises: tuple[Exception, ...] = (),
                 headers: dict | None = None, delay: float = 0.0, **kw) -> None:
        self.auths = 0
        self.sessions: list[FakeSession] = []
        self.body, self.statuses = body, list(statuses)
        self.raises, self.headers, self.delay = list(raises), headers, delay

        def auth():
            self.auths += 1
            return {"login": self.auths}

        def factory(auth_obj):
            s = FakeSession(self.body, self.statuses, auth_obj, self.raises, self.headers,
                            self.delay)
            self.sessions.append(s)
            return s

        kw.setdefault("backoff", 0.0)
        self.store = AssetStore(cache, auth=auth, session_factory=factory,
                                chunk_size=64 * 1024, **kw)

    @property
    def calls(self) -> list[dict]:
        return [c for s in self.sessions for c in s.calls]


# ------------------------------------------------------------------------------ cache layout
def test_cache_path_layouts(tmp_path):
    with_sum = asset_cache_path(tmp_path, URL, f"sha512:{SHA}")
    assert with_sum == tmp_path / SHA[:2] / f"{SHA[:16]}_X_001_gran.nc"
    assert asset_cache_path(tmp_path, URL, SHA) == with_sum              # bare hex accepted
    assert asset_cache_path(tmp_path, URL, f"SHA512:{SHA.upper()}") == with_sum
    key = hashlib.sha256(URL.encode()).hexdigest()[:16]
    assert asset_cache_path(tmp_path, URL, None) == tmp_path / "uri" / f"{key}_X_001_gran.nc"
    assert asset_cache_path(tmp_path, "https://h/a%20b.nc", None).name.endswith("_a b.nc")
    assert asset_cache_path(tmp_path, "https://h/", None).name.endswith("_asset")
    # without a checksum the ETag is folded into the key, so a re-delivery under the same URL
    # with a new ETag is a miss; with a checksum the digest is the whole identity
    tagged = asset_cache_path(tmp_path, URL, None, etag="v1")
    assert tagged.parent == tmp_path / "uri" and tagged != asset_cache_path(tmp_path, URL, None)
    assert tagged != asset_cache_path(tmp_path, URL, None, etag="v2")
    assert asset_cache_path(tmp_path, URL, SHA, etag="v1") == with_sum


def test_trusted_hosts():
    assert is_trusted_host(URL)
    assert is_trusted_host("https://urs.earthdata.nasa.gov/oauth")
    assert is_trusted_host("https://EarthdataCloud.nasa.gov/x")
    assert not is_trusted_host(FOREIGN)
    assert not is_trusted_host("https://earthdata.nasa.gov.evil.example/x")
    assert not is_trusted_host("https://notearthdata.nasa.gov/x")
    assert not is_trusted_host("https://nasa.gov/x")


def test_parse_checksum():
    assert parse_checksum(None) is None and parse_checksum("") is None
    assert parse_checksum(f"sha512:{SHA}") == ("sha512", SHA)
    assert parse_checksum(SHA) == ("sha512", SHA)
    sha256 = hashlib.sha256(b"x").hexdigest()
    assert parse_checksum(sha256) == ("sha256", sha256)
    for bad in ("md5", "abc", "sha512:xyz", "nope:" + SHA):
        with pytest.raises(ValueError):
            parse_checksum(bad)


# ---------------------------------------------------------------------------- miss, hit, agree
def test_miss_downloads_to_documented_path(tmp_path):
    cache = tmp_path / "assets"
    h = Harness(cache)
    handle = h.store.open(URL, checksum=f"sha512:{SHA}")
    assert isinstance(handle, CachedAsset) and isinstance(handle, LocalAsset)
    expected = cache / SHA[:2] / f"{SHA[:16]}_X_001_gran.nc"
    assert handle.path() == expected and handle.vsi() == str(expected)
    assert handle.uri == URL and handle.etag is None and handle.checksum == f"sha512:{SHA}"
    assert expected.read_bytes() == BODY
    assert h.auths == 1 and len(h.calls) == 1
    assert h.calls[0] == {"url": URL, "stream": True, "timeout": h.store.timeout}
    assert h.sessions[0].responses[0].closed
    assert not list(expected.parent.glob("*.part-*"))                  # temp cleaned up
    assert h.store.stage(URL, checksum=SHA) == expected                 # stage = open().path()


def test_hit_makes_no_request(tmp_path):
    cache = tmp_path / "assets"
    h = Harness(cache)
    h.store.open(URL, checksum=SHA)
    again = h.store.open(URL, checksum=SHA)
    assert again.path().read_bytes() == BODY
    assert len(h.calls) == 1 and h.auths == 1

    # a second store over the same cache directory (another worker on the node) agrees and
    # never touches the network or logs in
    other = Harness(cache)
    third = other.store.open(URL, checksum=f"sha512:{SHA}")
    assert third.path() == again.path()
    assert other.calls == [] and other.auths == 0 and other.sessions == []


def test_no_checksum_uses_uri_keyed_path(tmp_path):
    cache = tmp_path / "assets"
    h = Harness(cache)
    handle = h.store.open(URL)
    key = hashlib.sha256(URL.encode()).hexdigest()[:16]
    assert handle.path() == cache / "uri" / f"{key}_X_001_gran.nc"
    assert handle.checksum is None and handle.path().read_bytes() == BODY
    assert h.store.open(URL).path() == handle.path() and len(h.calls) == 1
    # an ETag changes the key: a new ETag re-downloads, the old one is still a hit
    v1 = h.store.open(URL, etag="v1")
    assert v1.path() != handle.path() and v1.etag == "v1" and len(h.calls) == 2
    assert h.store.open(URL, etag="v2").path() != v1.path() and len(h.calls) == 3
    assert h.store.open(URL, etag="v1").path() == v1.path() and len(h.calls) == 3


def test_stale_partial_file_is_ignored_and_left_unread(tmp_path):
    """12 section 8 question 3: only the final name is ever a hit; `.part-*` litter from a
    worker that died is neither trusted nor a reason not to download."""
    cache = tmp_path / "assets"
    target = asset_cache_path(cache, URL, SHA)
    target.parent.mkdir(parents=True)
    litter = target.with_name(f"{target.name}.part-1234")
    litter.write_bytes(b"half a file from a dead worker")
    h = Harness(cache)
    handle = h.store.open(URL, checksum=SHA)
    assert handle.path() == target and target.read_bytes() == BODY and len(h.calls) == 1
    assert litter.read_bytes() == b"half a file from a dead worker"     # untouched
    assert sorted(p.name for p in target.parent.iterdir()) == sorted([target.name, litter.name])


def test_two_stores_racing_on_one_target_both_succeed(tmp_path):
    """12 section 8 question 3: two workers may fetch the same asset; each renames a complete,
    verified copy, the last rename wins, nothing partial is ever visible."""
    cache = tmp_path / "assets"
    stores = [Harness(cache, delay=0.002) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        handles = list(pool.map(lambda h: h.store.open(URL, checksum=SHA), stores))
    assert len({hd.path() for hd in handles}) == 1
    assert handles[0].path().read_bytes() == BODY
    assert [len(h.calls) for h in stores] == [1, 1]                       # both downloaded
    assert not list(cache.rglob("*.part-*"))


def test_prefetch_stages_in_parallel_with_one_login(tmp_path):
    """12 section 7: the planner stages many assets through a thread pool; one login, the
    cached one costs nothing, a local URI is skipped, and every later open is a hit."""
    cache = tmp_path / "assets"
    h = Harness(cache, delay=0.001)
    h.store.open(URL, checksum=SHA)
    urls = [(URL, SHA), (URL + "?n=1", None), (URL + "?n=2", None), (URL + "?n=3", None),
            (str(tmp_path), None)]
    assert h.store.prefetch(urls, workers=3) == 3
    assert h.auths == 1 and len(h.calls) == 4 and not list(cache.rglob("*.part-*"))
    for u, c in urls[:4]:
        assert h.store.open(u, checksum=c).path().read_bytes() == BODY
    assert len(h.calls) == 4
    assert h.store.prefetch(urls) == 0


# ------------------------------------------------------------------------------- verification
def test_wrong_checksum_raises_and_leaves_nothing(tmp_path):
    cache = tmp_path / "assets"
    h = Harness(cache)
    wrong = hashlib.sha512(b"something else").hexdigest()
    with pytest.raises(ChecksumMismatch, match="12 section 4") as exc:
        h.store.open(URL, checksum=f"sha512:{wrong}")
    assert URL in str(exc.value) and SECRET not in str(exc.value)
    target = cache / wrong[:2]
    assert not list(cache.rglob("*")) or all(p.is_dir() for p in cache.rglob("*"))
    assert not target.exists() or not any(target.iterdir())
    # the next open retries the download rather than trusting a bad copy
    h.store.open(URL, checksum=SHA)
    assert len(h.calls) == 2


def test_non_2xx_raises_with_status(tmp_path):
    h = Harness(tmp_path / "assets", statuses=(404,))
    with pytest.raises(AssetFetchError) as exc:
        h.store.open(URL, checksum=SHA)
    assert exc.value.status == 404 and exc.value.uri == URL and "404" in str(exc.value)
    assert not exc.value.transient and len(h.calls) == 1                 # no retry on a 404
    assert not (tmp_path / "assets" / SHA[:2]).exists() or \
        not any((tmp_path / "assets" / SHA[:2]).iterdir())
    assert h.auths == 1                                                  # a 404 is not an auth failure


def test_html_body_is_never_cached_as_an_asset(tmp_path):
    """A 200 login page (Earthdata Login redirecting an unauthenticated request) is refused,
    checksum or not; nothing lands in the cache."""
    h = Harness(tmp_path / "assets", headers={"Content-Type": "text/html; charset=utf-8"})
    with pytest.raises(AssetFetchError, match="text/html") as exc:
        h.store.open(URL)
    assert exc.value.status == 200 and not exc.value.transient
    assert not list((tmp_path / "assets").rglob("*gran.nc*"))


# ---------------------------------------------------------------------------------- retries
def test_transient_statuses_are_retried_then_succeed(tmp_path):
    """08 section 4: a 503 from the archive is retried with backoff, not a failed run."""
    h = Harness(tmp_path / "assets", statuses=(503, 429, 502, 200))
    handle = h.store.open(URL, checksum=SHA)
    assert handle.path().read_bytes() == BODY
    assert [c["url"] for c in h.calls] == [URL] * 4 and h.auths == 1
    assert not list((tmp_path / "assets").rglob("*.part-*"))


def test_transient_failures_are_bounded(tmp_path):
    h = Harness(tmp_path / "assets", statuses=(503,), retries=2)
    with pytest.raises(AssetFetchError) as exc:
        h.store.open(URL, checksum=SHA)
    assert exc.value.status == 503 and exc.value.transient and len(h.calls) == 3
    assert not list((tmp_path / "assets").rglob("*gran.nc*"))


def test_transport_failure_is_wrapped_and_retried(tmp_path):
    """A requests exception (timeout, reset) becomes `AssetFetchError(status=None)` naming
    the type, is retried, and never reaches a worker as a raw traceback."""
    h = Harness(tmp_path / "assets", raises=(ReadTimeout("boom " + SECRET),))
    handle = h.store.open(URL, checksum=SHA)
    assert handle.path().read_bytes() == BODY and len(h.calls) == 2
    h2 = Harness(tmp_path / "assets2", raises=tuple(ReadTimeout("x") for _ in range(9)),
                 retries=1)
    with pytest.raises(AssetFetchError) as exc:
        h2.store.open(URL, checksum=SHA)
    assert exc.value.status is None and "no response" in str(exc.value)
    assert "ReadTimeout" in str(exc.value) and SECRET not in str(exc.value)
    assert len(h2.calls) == 2


# ------------------------------------------------------------------------- credential scope
def test_http_is_refused_before_any_login(tmp_path):
    """12 section 4: a plaintext URL would carry the bearer token in the clear."""
    h = Harness(tmp_path / "assets")
    with pytest.raises(UntrustedScheme, match="12 section 4"):
        h.store.open(URL.replace("https://", "http://"), checksum=SHA)
    with pytest.raises(UntrustedScheme):
        h.store.credentials_for("http://data.lpdaac.earthdatacloud.nasa.gov/x.nc")
    assert h.auths == 0 and h.sessions == []


def test_foreign_host_never_sees_the_credential(tmp_path):
    """12 section 4: the Earthdata session goes only to Earthdata hosts. An index row naming
    another host is fetched anonymously - no login, no Authorization header - and a 401 there
    is final, not a re-login."""
    h = Harness(tmp_path / "assets")
    handle = h.store.open(FOREIGN, checksum=SHA)
    assert handle.path().read_bytes() == BODY
    assert h.auths == 0 and h.store.credentials_for(FOREIGN).kind == "none"
    (anon,) = h.sessions
    assert anon.auth_obj is None and "Authorization" not in anon.headers
    assert anon.calls[0]["url"] == FOREIGN
    # a trusted host afterwards logs in and gets its own session, with the credential
    h.store.open(URL)
    assert h.auths == 1 and len(h.sessions) == 2 and h.sessions[1].headers["Authorization"] == SECRET
    denied = Harness(tmp_path / "assets3", statuses=(401,))
    with pytest.raises(AssetFetchError) as exc:
        denied.store.open(FOREIGN)
    assert exc.value.status == 401 and denied.auths == 0 and len(denied.calls) == 1


# ---------------------------------------------------------------------------------- re-login
def test_401_relogins_once_and_retries(tmp_path):
    cache = tmp_path / "assets"
    h = Harness(cache, statuses=(401, 200))
    handle = h.store.open(URL, checksum=SHA)
    assert handle.path().read_bytes() == BODY
    assert h.auths == 2 and len(h.sessions) == 2                          # exactly one re-login
    assert [c["url"] for c in h.calls] == [URL, URL]
    assert h.sessions[0].responses[0].closed
    # the refreshed session is kept for later fetches: no third login
    h.store.open(URL + "?other=1", checksum=None)
    assert h.auths == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_twice_raises_without_credentials(tmp_path, status):
    h = Harness(tmp_path / "assets", statuses=(status, status))
    with pytest.raises(AssetFetchError) as exc:
        h.store.open(URL, checksum=SHA)
    msg = str(exc.value)
    assert exc.value.status == status and URL in msg and str(status) in msg
    assert SECRET not in msg and "not-a-real-token" not in msg
    assert h.auths == 2 and len(h.calls) == 2                             # once, then stop
    assert not list((tmp_path / "assets").rglob("*.nc"))


def test_default_auth_forces_relogin_on_retry(tmp_path, monkeypatch):
    """With no injected callables the store goes through `stratum.access.auth`: the first login
    is plain, the retry after a 401 is `earthdata_login(force=True)`, and the session is
    `earthdata_session(auth)`."""
    import stratum.access.auth as auth_mod

    logins: list[bool] = []
    statuses = [401, 200]

    def fake_login(*, strategies=auth_mod.STRATEGIES, force=False):
        logins.append(force)
        return {"auth": len(logins)}

    monkeypatch.setattr(auth_mod, "earthdata_login", fake_login)
    monkeypatch.setattr(auth_mod, "earthdata_session", lambda auth: FakeSession(BODY, statuses))
    store = AssetStore(tmp_path / "assets", chunk_size=64 * 1024)
    handle = store.open(URL, checksum=SHA)
    assert handle.path().read_bytes() == BODY
    assert logins == [False, True] and store.logins == 2


# ------------------------------------------------------------------------------ configuration
def test_asset_cache_env_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv(ASSET_CACHE_ENV, str(tmp_path / "from-env"))
    h = Harness(None)
    assert h.store.asset_cache == tmp_path / "from-env"
    handle = h.store.open(URL, checksum=SHA)
    assert handle.path().is_relative_to(tmp_path / "from-env")
    # an explicit argument wins over the environment
    assert AssetStore(tmp_path / "explicit").asset_cache == tmp_path / "explicit"


def test_https_without_cache_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv(ASSET_CACHE_ENV, raising=False)
    h = Harness(None)
    assert h.store.asset_cache is None
    with pytest.raises(AssetCacheUnconfigured, match=ASSET_CACHE_ENV):
        h.store.open(URL, checksum=SHA)
    with pytest.raises(AssetCacheUnconfigured):
        h.store.cache_path(URL)
    assert h.auths == 0 and h.calls == []                                 # fails before any login
    assert h.store.credentials_for(URL).kind in ("netrc", "bearer")


def test_s3_raises_not_implemented(tmp_path):
    h = Harness(tmp_path / "assets")
    with pytest.raises(NotImplementedError, match="12 section 4"):
        h.store.open("s3://lp-prod-protected/X.001/gran/X_001_gran.nc", checksum=SHA)
    with pytest.raises(NotImplementedError, match="12 section 4"):
        h.store.credentials_for("s3://lp-prod-protected/x.nc")
    with pytest.raises(NotImplementedError, match="12 section 4"):
        h.store.open("ftp://host/x.nc")
    assert h.auths == 0


def test_credentials_for_https(tmp_path, monkeypatch):
    store = AssetStore(tmp_path / "assets")
    rc = tmp_path / "netrc"
    rc.write_text("machine urs.earthdata.nasa.gov login u password p\n")
    rc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(rc))
    cred = store.credentials_for(URL)
    assert cred.kind == "netrc" and cred.expires is None and cred.as_env() == {}
    monkeypatch.setenv("NETRC", str(tmp_path / "absent"))
    assert store.credentials_for(URL).kind == "bearer"
    assert store.credentials_for(str(tmp_path)).kind == "none"


# ------------------------------------------------------------------------ local unchanged
def test_local_behaviour_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv(ASSET_CACHE_ENV, raising=False)
    f = tmp_path / "a.nc"
    f.write_bytes(b"x")
    h = Harness(None)
    store = h.store
    for uri in (str(f), f.as_uri()):
        handle = store.open(uri, etag=None)
        assert isinstance(handle, LocalAsset) and not isinstance(handle, CachedAsset)
        assert handle.uri == uri and handle.etag is None
        assert handle.path() == f.absolute() and handle.vsi() == str(f.absolute())
        assert store.stage(uri) == f.absolute()
        assert store.credentials_for(uri).kind == "none"
    # a checksum on a local file is identity, not something to verify: accepted and ignored
    assert store.open(str(f), checksum=SHA).path() == f.absolute()
    assert to_uri(f) == f.absolute().as_uri()
    with pytest.raises(FileNotFoundError):
        store.open(str(tmp_path / "missing.nc"))
    assert h.auths == 0 and h.sessions == []                              # never logged in
    # constructible with no arguments, as every existing caller does
    assert AssetStore().open(str(f)).path() == f.absolute()


def test_construction_is_lazy(tmp_path):
    calls = []
    store = AssetStore(tmp_path, auth=lambda: calls.append(1))
    assert calls == [] and store.logins == 0


# --------------------------------------------------------------------------------------- live
LIVE_URL = ("https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/EMITL2BMIN.001/"
            "EMIT_L2B_MIN_001_20260210T210747_2604114_008/"
            "EMIT_L2B_MIN_001_20260210T210747_2604114_008.nc")


@pytest.mark.skipif(os.environ.get("STRATUM_LIVE") != "1", reason="set STRATUM_LIVE=1")
def test_live_stage_from_lpdaac(tmp_path):
    """Downloads one 43 MB L2B MIN asset through the real Earthdata session. The checksum is
    not asserted here because the index carries it; this only proves the session, the
    streaming path and the cache layout against the real endpoint."""
    store = AssetStore(tmp_path / "assets")
    handle = store.open(LIVE_URL)
    assert handle.path().stat().st_size > 10_000_000
    assert store.credentials_for(LIVE_URL).kind in ("netrc", "bearer")
    assert store.open(LIVE_URL).path() == handle.path()
