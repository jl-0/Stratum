"""Earthdata Login, in one place (12 section 4 "Credentials"; 08 section 5).

`earthdata_login()` returns an `earthaccess.Auth`, trying the `netrc` strategy and then
`environment` (`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, or `EARTHDATA_TOKEN`). It is never
called at import time and never on a local-only run; the result is cached per process, and a
spawned worker logs in afresh on its first remote asset (08 section 1). It builds its own
`Auth` object rather than calling `earthaccess.login()`, which mutates module-level state that
a process pool and Lambda both get wrong (12 section 5, "Isolate global state").

Nothing here logs, stores or raises a token or a password: an exception names the strategies
tried and the exception *types* met, never their messages.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

STRATEGIES: tuple[str, ...] = ("netrc", "environment")

_auth: Any | None = None


class EarthdataLoginError(RuntimeError):
    """No login strategy produced an authenticated session."""


def earthdata_login(*, strategies: Sequence[str] = STRATEGIES, force: bool = False) -> Any:
    """The process's `earthaccess.Auth`, logging in on first call (12 section 4).

    `force` discards the cached object and logs in again - what `AssetStore` does once after a
    401/403, the credential-expiry path of 08 section 5.
    """
    global _auth
    if _auth is not None and not force and getattr(_auth, "authenticated", False):
        return _auth
    from earthaccess import Auth  # deferred: a local-only run never imports earthaccess

    tried: list[str] = []
    for strategy in strategies:
        auth = Auth()
        try:
            auth.login(strategy=strategy)
        except Exception as e:  # noqa: BLE001 - the type is reported, the message never is
            tried.append(f"{strategy} ({type(e).__name__})")
            continue
        if getattr(auth, "authenticated", False):
            log.info("Earthdata Login: authenticated via %s", strategy)
            _auth = auth
            return auth
        tried.append(f"{strategy} (not authenticated)")
    raise EarthdataLoginError(
        "Earthdata Login failed; strategies tried: " + ", ".join(tried)
        + ". Put credentials in ~/.netrc for urs.earthdata.nasa.gov or set EARTHDATA_USERNAME "
        "and EARTHDATA_PASSWORD (12 section 4)")


def earthdata_session(auth: Any | None = None) -> Any:
    """A `requests.Session` carrying the login - `auth.get_session()` - for HTTPS assets behind
    Earthdata Login. Logs in when no `auth` is given."""
    auth = auth if auth is not None else earthdata_login()
    return auth.get_session()


def reset_login() -> None:
    """Forget the cached login (tests, and a worker that wants a clean slate)."""
    global _auth
    _auth = None


__all__ = ["STRATEGIES", "EarthdataLoginError", "earthdata_login", "earthdata_session",
           "reset_login"]
