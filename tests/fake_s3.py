"""An in-memory S3 standing in for boto3, backed by a directory.

Only the four calls `stratum.storage.ObjectStore` makes are implemented, and they are implemented
strictly: an unknown key raises the same `ClientError` shape botocore raises, so the module under
test cannot accidentally pass by treating a miss as a hit.

`install(monkeypatch, root)` replaces `boto3.client` for the duration of a test. It returns the
fake, whose `objects` property is every key that has been written - which is how the tests assert
ordering (`.inputs.json` last) without reaching into the implementation.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """One bucket per name, each a directory under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.puts: list[str] = []   # every key written, in order
        self.gets: list[str] = []

    # -- helpers ---------------------------------------------------------------------------
    def _path(self, bucket: str, key: str) -> Path:
        return self.root / bucket / key

    @property
    def objects(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    # -- the boto3 surface -----------------------------------------------------------------
    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        path = self._path(Bucket, Key)
        if not path.is_file():
            raise FakeClientError("404")
        return {"ContentLength": path.stat().st_size}

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        path = self._path(bucket, key)
        if not path.is_file():
            raise FakeClientError("404")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        self.gets.append(key)

    def upload_file(self, src: str, bucket: str, key: str) -> None:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        self.puts.append(key)

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return FakePaginator(self)


class FakePaginator:
    def __init__(self, client: FakeS3) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str,
                 Delimiter: str | None = None) -> list[dict[str, Any]]:
        base = self.client.root / Bucket
        if not base.is_dir():
            return [{}]
        keys = sorted(str(p.relative_to(base).as_posix()) for p in base.rglob("*") if p.is_file())
        hits = [k for k in keys if k.startswith(Prefix)]
        if Delimiter is None:
            return [{"Contents": [{"Key": k} for k in hits]}] if hits else [{}]
        # A delimited list: keys with no further delimiter come back as Contents, and everything
        # deeper is rolled up into CommonPrefixes. S3 does exactly this, and `ObjectStore.list_dirs`
        # depends on it - a fake that returned every key would let a broken caller pass.
        contents, prefixes = [], set()
        for k in hits:
            rest = k[len(Prefix):]
            if Delimiter in rest:
                prefixes.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
            else:
                contents.append({"Key": k})
        page: dict[str, Any] = {}
        if contents:
            page["Contents"] = contents
        if prefixes:
            page["CommonPrefixes"] = [{"Prefix": p} for p in sorted(prefixes)]
        return [page] if page else [{}]


def install(monkeypatch: Any, root: Path) -> FakeS3:
    """Patch `boto3.client` and botocore's `ClientError` so `ObjectStore` talks to the fake."""
    import boto3
    import botocore.exceptions

    fake = FakeS3(root)
    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: fake)
    monkeypatch.setattr(botocore.exceptions, "ClientError", FakeClientError)
    return fake
