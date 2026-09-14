# stratum-emit

EMIT readers, instrument masks and mineral scorers for [Stratum](../../README.md).

This is a separate distribution from `stratum`, and that is the point: it is the shape any
project-specific plugin package takes. The framework meets it exactly as it would meet a package
from another project — through entry points read from installed metadata, never through an import.
`src/stratum/` contains no EMIT, Tetracorder or mineral knowledge and a test enforces it
(`tests/test_docs_developer.py::test_the_framework_never_imports_the_plugin_package`).

## What it registers

| Group | Names |
|---|---|
| `stratum.scorers` | `min_view_zenith`, `cleanest_nadir`, `prefer_bare_earth`, `max_band_depth` |
| `stratum.masks` | `edge_trim`, `slit_dust`, `l2a_standard`, `soil_fraction` |
| `stratum.readers` | `EMITL2BMIN`, `EMITL1BRAD`, `EMITL2AMASK`, `EMITL2BFRCOV` |

A manifest refers to any of them by name. `stratum plugins list` enumerates what an installed
environment or an image actually provides.

## Writing one of your own

1. A distribution with a `[project.entry-points."stratum.scorers"]` table (or `masks`, `readers`,
   `filters`, `mappers`, `reducers`) mapping a name to `module:Class`.
2. Classes satisfying the protocols in `stratum.hooks` / `stratum.types`.
3. Install it beside `stratum` — in the container image at build time, so the deployment's image
   digest identifies the code that ran.

No infrastructure change is involved at any point: a plugin is code and configuration, never a
Terraform resource ([ADR-0003](../../docs/decisions/ADR-0003-image-build-and-digest.md)).
