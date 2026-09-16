"""Static validation: every 09 section 5 check that needs no data.

Returns problems as a list so the planner and `stratum validate` can print them all. What needs
an index, a granule or an aux file - vintage agreement, `var` existence, URI credentials, aux
readability, per-granule enumeration resolution - belongs to the planner.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stratum.manifest.models import CONFIG_MAPPERS, Manifest
from stratum.plugins import resolve
from stratum.publish.cogs import check_formats
from stratum.reduce import SchemaError, product_bands
from stratum.types import BandSpec


def _instantiate(kind: str, ref: str, params: dict[str, Any], where: str,
                 problems: list[str]) -> Any | None:
    try:
        cls = resolve(kind, ref)
    except (LookupError, ImportError, AttributeError) as e:
        problems.append(f"{where}: {kind} {ref!r} does not resolve: {e}")
        return None
    try:
        return cls(**params)
    except TypeError as e:
        problems.append(f"{where}: {kind} {ref!r} rejects params {params}: {e}")
        return cls


def _roles_and_aux(obj: Any, where: str, m: Manifest, problems: list[str]) -> None:
    names = m.inputs.names
    for role in getattr(obj, "required_roles", ()) or ():
        if role not in names:
            problems.append(f"{where}: required role {role!r} is not in inputs.roles or "
                            "inputs.band_aliases")
    for alias in getattr(obj, "required_aux", ()) or ():
        if alias not in m.aux:
            problems.append(f"{where}: required aux {alias!r} is not declared in aux "
                            "(05 section 5)")
    if getattr(obj, "space", None) == "sensor" and getattr(obj, "required_aux", ()):
        problems.append(f"{where}: a sensor-space mask cannot declare required_aux - aux is on "
                        "the BLOCK grid (05 section 1) and this mask sees sensor geometry")


#: Aux source kinds the accessor can actually serve (05 section 3). The rest of `AuxAccessor`
#: - vector, features, distance, table - is specified and not built, so a manifest naming one is
#: refused here rather than in a worker.
AUX_KINDS_BUILT = ("continuous", "categorical")


def _aux_source_problems(alias: str, spec: Any) -> list[str]:
    """One declared aux source, checked without opening it (05 section 2, 5).

    `kind` vs `resampling` agreement is already enforced by `AuxSpec` itself, so what is left is
    what this slice can execute: a raster, from a scheme the asset store can stage, with no date
    templating.
    """
    problems: list[str] = []
    where = f"aux.{alias}"
    if spec.kind not in AUX_KINDS_BUILT:
        problems.append(f"{where}: kind {spec.kind!r} is not built; the accessor serves "
                        f"{list(AUX_KINDS_BUILT)} through raster() and vector/features/distance/"
                        "table still raise (05 section 3)")
    if spec.temporal is not None:
        problems.append(f"{where}: temporal {spec.temporal!r} is not built (05 section 2). "
                        "Resolving a date against `{date}` means finding the nearest date that "
                        "EXISTS, which needs a listing, and a run never queries a catalogue "
                        "(02 section 6); declare a static uri")
    for uri in spec.uris:
        scheme = str(uri).split("://", 1)[0].lower() if "://" in str(uri) else "file"
        if scheme not in ("https", "file"):
            problems.append(f"{where}: uri scheme {scheme!r} is not supported; aux is staged "
                            "through the asset store, which opens https:// and file:// "
                            "(12 section 4). s3:// direct access is in-region only, not built")
    return problems


def _reader_space(collection: str, overrides: Mapping[str, str]) -> str | None:
    """"sensor" | "ortho" for the reader registered for `collection`, or None when none is."""
    from stratum.access import reader_for

    try:
        return str(getattr(reader_for(collection, overrides), "space", "sensor"))
    except (LookupError, ImportError, AttributeError, TypeError):
        return None


def _role_space_problems(m: Manifest) -> list[str]:
    """`resampling` is required for an ortho-native role and refused for a sensor-space one
    (12 section 2, under 05 section 2's declared-never-defaulted rule).

    Checkable without data: a reader is selected from what the role DECLARES, so the registry
    answers "is this collection ortho?" from entry-point metadata alone (12 section 3 rule 1). A
    collection with no reader is left alone - `_instantiate`-style resolution failures are the
    planner's to report, and duplicating them here would say the same thing twice.
    """
    problems: list[str] = []
    for name, role in m.inputs.roles.items():
        space = _reader_space(role.collection, m.inputs.readers)
        if space is None:      # no reader resolves; the planner reports that, not this
            continue
        where = f"inputs.roles.{name}"
        if space == "ortho" and role.resampling is None:
            problems.append(f"{where}: collection {role.collection!r} is ortho-native, so the "
                            "role must declare `resampling` - the framework warps it onto the "
                            "block grid and will not guess how (12 section 2, 05 section 2)")
        elif space != "ortho" and role.resampling is not None:
            problems.append(f"{where}: `resampling` applies to an ortho-native role only; "
                            f"{role.collection!r} is read in sensor space and gathered through a "
                            "GLT (12 section 2)")
    return problems


def _reducer_problems(ref: str, reducer: Any) -> list[str]:
    """What a Reducer plugin must declare before a worker will run it (04 section 5).

    Its `outputs` are the contract - there is no schema to fall back on, and publish stitches
    exactly the bands it names - so a bad declaration fails here rather than after the first
    block has been reduced.
    """
    problems: list[str] = []
    outputs = getattr(reducer, "outputs", None)
    if not outputs:
        problems.append(f"reducer {ref}: declares no `outputs`; a Reducer names the bands it "
                        "delivers up front, so a bad declaration fails at plan time "
                        "(04 section 5)")
    else:
        if not all(isinstance(b, BandSpec) for b in outputs):
            problems.append(f"reducer {ref}: every entry of `outputs` must be a BandSpec")
        else:
            names = [b.name for b in outputs]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                problems.append(f"reducer {ref}: `outputs` names {dupes} more than once; one "
                                "band, one name")
    halo = getattr(reducer, "halo", 0)
    if isinstance(halo, int) and halo:
        problems.append(
            f"reducer {ref}: halo {halo} is not built. Snapshots are written at their block's "
            "CORE extent and their cache key excludes the halo, so a reducer that needs "
            "neighbouring cells would have to read and stitch the surrounding blocks "
            "(04 section 5, 06 section 2); declare halo = 0")
    if getattr(reducer, "required_aux", ()):
        problems.append(f"reducer {ref}: aux in the reduce stage is not built; the Reducer "
                        "protocol declares no required_aux and reduce() receives NullAux "
                        "(05 section 3)")
    return problems


def _delivered_names(m: Manifest, problems: list[str], reducer: Any = None) -> set[str]:
    """Band names the built-in reducer delivers for this schema - the actual rule
    (`stratum.reduce.delivered_bands`, 13 section 4), not every suffix for every layer, so an
    `alpha_from` naming a band the reducer never writes fails here and not at publish."""
    try:
        return {b.name for b in product_bands(m.snapshot_schema(), reducer)}
    except SchemaError as e:
        problems.append(f"snapshot: {e}")
        return set()


def validate_static(m: Manifest) -> list[str]:
    """Problems that make a manifest unrunnable regardless of data; empty means ok."""
    problems: list[str] = []
    inputs = m.inputs

    # -- one namespace for roles and aliases (13 section 2)
    collide = set(inputs.roles) & set(inputs.band_aliases)
    if collide:
        problems.append(f"inputs: {sorted(collide)} are both a role and a band alias")
    for alias, spec in inputs.band_aliases.items():
        if spec.role not in inputs.roles:
            problems.append(f"inputs.band_aliases.{alias}: role {spec.role!r} is not declared")
    if inputs.geolocation is not None and inputs.geolocation not in inputs.roles:
        problems.append(f"inputs.geolocation {inputs.geolocation!r} is not a role")

    # -- plugins resolve; required roles and aux declared (04 section 8)
    scorer = _instantiate("scorer", m.scorer.ref, m.scorer.params, "scorer", problems)
    if scorer is not None:
        _roles_and_aux(scorer, f"scorer {m.scorer.ref}", m, problems)
        halo = getattr(scorer, "halo", 0)
        if isinstance(halo, int) and halo * 2 >= m.grid.block_size:
            problems.append(f"scorer {m.scorer.ref}: halo {halo} is not consistent with block "
                            f"{m.grid.block_size}")
        if getattr(scorer, "capability", "streaming") != "streaming":
            problems.append(f"scorer {m.scorer.ref}: capability "
                            f"{scorer.capability!r} is a later slice (plan section 1)")
    for i, mask in enumerate(m.pixel_mask):
        inst = _instantiate("mask", mask.ref, mask.params, f"pixel_mask[{i}]", problems)
        if inst is not None:
            _roles_and_aux(inst, f"pixel_mask[{i}] {mask.ref}", m, problems)
    reducer = None
    if m.reducer is not None:
        reducer = _instantiate("reducer", m.reducer.ref, m.reducer.params, "reducer", problems)
        if reducer is not None:
            problems.extend(_reducer_problems(m.reducer.ref, reducer))
    for i, f in enumerate(m.granule_filter):
        if f.ref is not None:
            _instantiate("filter", f.ref, f.params, f"granule_filter[{i}]", problems)

    # -- the snapshot schema resolves against the namespace (13 section 2)
    layers = m.snapshot.layers
    for name, layer in layers.items():
        where = f"snapshot.layers.{name}"
        if layer.source not in inputs.names:
            problems.append(f"{where}: source {layer.source!r} is neither a role nor a band alias")
        elif layer.kind == "categorical":
            role = inputs.roles.get(layer.source)
            if role is not None and role.class_table is None and layer.classes is not None:
                problems.append(f"{where}: role {layer.source!r} declares no class_table, so "
                                "its classes cannot be resolved (11 section 9)")
        a = layer.aggregate
        if a.conditional_on is not None:
            other = layers.get(a.conditional_on)
            if other is None or other.kind != "categorical" or a.conditional_on == name:
                problems.append(f"{where}: conditional_on {a.conditional_on!r} must name another "
                                "categorical layer")
            elif other.aggregate.method == "none":
                problems.append(f"{where}: conditional_on {a.conditional_on!r} is carried, not "
                                "delivered (aggregate none), so it has no winner to condition on")
        if a.unc is not None:
            other = layers.get(a.unc)
            if other is None or other.kind != "continuous" or a.unc == name:
                problems.append(f"{where}: unc {a.unc!r} must name another continuous layer")
        if a.ignore:
            enum = m.enumerations().get(name)
            if enum is not None:
                unknown = [c for c in a.ignore if c not in enum.names]
                if unknown:
                    problems.append(f"{where}: ignore names {unknown} not in enumeration "
                                    f"{enum.name!r}")
    for name, enum in m.enumerations().items():
        role = inputs.roles.get(layers[name].source)
        if role is not None and role.class_table is not None:
            missing = [a for a in enum.match_on if a not in role.class_table.attributes]
            if missing:
                problems.append(f"snapshot.layers.{name}: match_on {missing} are not in "
                                f"inputs.roles.{layers[name].source}.class_table.attributes")

    # -- what this slice cannot execute is refused here, not in a worker (first-slice plan
    #    section 1; 09 section 5 "a typo fails in the planner, not in workers")
    for alias, spec in m.aux.items():
        problems.extend(_aux_source_problems(alias, spec))
    problems.extend(_role_space_problems(m))
    try:
        check_formats(list(m.outputs.formats))
    except (NotImplementedError, ValueError) as e:
        problems.append(str(e))

    # -- render names layers and their classes (07 section 3, 09 section 5)
    delivered = _delivered_names(m, problems, reducer)
    for band, r in m.outputs.render.items():
        where = f"outputs.render.{band}"
        target = r.layer or band
        layer = layers.get(target)
        if layer is None:
            problems.append(f"{where}: {target!r} is not a snapshot layer")
        elif r.mapper == "categorical" and layer.kind != "categorical":
            problems.append(f"{where}: categorical mapper over continuous layer {target!r}")
        elif r.mapper == "continuous" and layer.kind != "continuous":
            problems.append(f"{where}: continuous mapper over categorical layer {target!r}")
        elif layer.aggregate.method == "none":
            problems.append(f"{where}: layer {target!r} is carried, not delivered (aggregate none)")
        if r.colors and layer is not None and layer.kind == "categorical":
            enum = m.enumerations().get(target)
            if enum is not None:
                unknown = [c for c in r.colors if c not in enum.names]
                if unknown:
                    problems.append(f"{where}: colors name classes {unknown} not in enumeration "
                                    f"{enum.name!r} (07 section 6)")
        if r.alpha_from is not None and r.alpha_from.band not in delivered:
            problems.append(f"{where}: alpha_from band {r.alpha_from.band!r} is not a delivered "
                            f"band; choose from {sorted(delivered)}")
        if r.mapper not in CONFIG_MAPPERS:
            _instantiate("mapper", r.mapper, r.params, where, problems)

    # -- area
    if m.aoi.zones is not None:
        try:
            m.aoi_bbox()
        except (ValueError, TypeError) as e:
            problems.append(f"aoi: {e}")

    return problems
