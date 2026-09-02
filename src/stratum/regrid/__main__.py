"""`python -m stratum.regrid [--record]`: check or rewrite ALGO_HASH (06 section 3, rule 2).

Without a flag it reports whether the recorded hash matches and exits 1 when it does not; with
`--record` it rewrites the file for the current REGRID_ALGO_VERSION and module content. See the
package docstring for when each is the right step.
"""
from __future__ import annotations

import sys

from stratum.regrid import ALGO_HASH_PATH, check_recorded_hash, record_hash


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--record"]:
        version, digest = record_hash()
        print(f"recorded {version} {digest} -> {ALGO_HASH_PATH}")
        return 0
    if args:
        print("usage: python -m stratum.regrid [--record]", file=sys.stderr)
        return 2
    ok, message = check_recorded_hash()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
