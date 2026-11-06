"""Give each demonstration run the key the comparison report uses, as its STAC collection `title`.

    pixi run python examples/emit-cm-demo-sites/set_titles.py examples/emit-cm-demo-sites/out/products
    AWS_PROFILE=mosaic-gen pixi run python examples/emit-cm-demo-sites/set_titles.py \
        s3://stratum-emit-dev-311456830552/products [--dry-run]

The viewer lists a run by its collection `title` when it has one (docs/guide/viewing.html), so
the run picker reads "Y0 - yearly-base" rather than the run id. Nothing else changes: the run id,
its prefix and every item stay as published. Publish does not write a title, so re-run this after
re-publishing a run. A run whose manifest `run_id` is not listed below is left alone.
"""
import json
import sys
from pathlib import Path

#: manifest run_id -> the report's key (Demo-Sites-Comparison, section 02)
KEYS = {
    "cm-demo-yearly-base-2025": "Y0 - yearly-base",
    "cm-demo-yearly-min3-2025": "Y1 - yearly-min3",
    "cm-demo-yearly-nondetect-vote-2025": "Y2 - yearly-nondetect-vote",
    "cm-demo-yearly-weighted-2025": "Y3 - yearly-weighted",
    "cm-demo-yearly-reference-2025": "Y4 - yearly-reference",
    "cm-demo-baseline-2025": "A - baseline",
    "cm-demo-mode-gated-2025": "B - mode-gated",
    "cm-demo-assistant-pick-2025": "C - assistant-pick",
    "cm-demo-confirmed-2025": "D - confirmed",
    "cm-demo-score-weighted-2025": "E - score-weighted",
}


def key_for(run: str) -> str | None:
    """A published run is `<run_id>-<YYYYMMDD>-<hash>`."""
    base = run.rsplit("-", 2)[0]
    return KEYS.get(base)


def retitle(doc: dict, title: str) -> bool:
    if doc.get("title") == title:
        return False
    doc["title"] = title
    return True


def main(root: str, dry: bool) -> None:
    if root.startswith("s3://"):
        import boto3
        bucket, _, prefix = root[5:].partition("/")
        prefix = prefix.rstrip("/") + "/"
        s3 = boto3.client("s3")
        runs = [p["Prefix"][len(prefix):].rstrip("/") for page in
                s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix,
                                                             Delimiter="/")
                for p in page.get("CommonPrefixes", ())]
        for run in sorted(runs):
            title = key_for(run)
            if not title:
                continue
            k = f"{prefix}{run}/collection.json"
            doc = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            if retitle(doc, title) and not dry:
                s3.put_object(Bucket=bucket, Key=k, Body=json.dumps(doc, indent=1).encode(),
                              ContentType="application/json")
            print(f"{'would set' if dry else 'set'}  {title:28s} {run}")
    else:
        for run in sorted(Path(root).iterdir()):
            title = key_for(run.name)
            f = run / "collection.json"
            if not title or not f.exists():
                continue
            doc = json.loads(f.read_text())
            if retitle(doc, title) and not dry:
                f.write_text(json.dumps(doc, indent=1))
            print(f"{'would set' if dry else 'set'}  {title:28s} {run.name}")


if __name__ == "__main__":
    main(sys.argv[1], "--dry-run" in sys.argv[2:])
