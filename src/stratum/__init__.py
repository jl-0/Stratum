"""Stratum - a cost-function-driven mosaic engine for imaging spectroscopy.

Nothing in this package knows what a mineral, a wavelength or an instrument is. Every science
decision arrives through a hook (`stratum.hooks`); every byte arrives through a reader
(`stratum.types.GranuleReader`). Domain knowledge lives in plugin packages such as `stratum_emit`.

Specs: docs/specs/. Once code exists it is authoritative for signatures; the specs stay the
narrative (CLAUDE.md, prime directive).
"""

__version__ = "0.0.1"
