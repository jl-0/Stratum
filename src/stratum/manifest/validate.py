"""Static validation: every 09 section 5 check that needs no data.

Returns problems as a list so the planner and `stratum validate` can print them all. What needs
an index, a granule or an aux file - vintage agreement, `var` existence, URI credentials, aux
readability, per-granule enumeration resolution - belongs to the planner.
"""
from __future__ import annotations

from typing import Any

from stratum.manifest.models import CONFIG_MAPPERS, Manifest
from stratum.plugins import resolve
from stratum.publish.cogs import check_formats
from stratum.reduce import SchemaError, delivered_bands


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
        else:
            problems.append(f"{where}: required aux {alias!r} is declared, but aux data is not "
                            "in this slice (05; first-slice plan section 1)")


def _delivered_names(m: Manifest, problems: list[str]) -> set[str]:
    """Band names the built-in reducer delivers for this schema - the actual rule
    (`stratum.reduce.delivered_bands`, 13 section 4), not every suffix for every layer, so an
    `alpha_from` naming a band the reducer never writes fails here and not at publish."""
    try:
        return {b.name for b in delivered_bands(m.snapshot_schema())}
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
        if isinstance(halo, int) and halo * 2 >= m.grid.block:
            problems.append(f"scorer {m.scorer.ref}: halo {halo} is not consistent with block "
                            f"{m.grid.block}")
        if getattr(scorer, "capability", "streaming") != "streaming":
            problems.append(f"scorer {m.scorer.ref}: capability "
                            f"{scorer.capability!r} is a later slice (plan section 1)")
    for i, mask in enumerate(m.pixel_mask):
        inst = _instantiate("mask", mask.ref, mask.params, f"pixel_mask[{i}]", problems)
        if inst is not None:
            _roles_and_aux(inst, f"pixel_mask[{i}] {mask.ref}", m, problems)
    if m.reducer is not None:
        problems.append(f"reducer {m.reducer.ref!r}: Reducer plugins are a later slice "
                        "(plan section 1); the schema vocabulary runs when reducer is omitted")
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
    if m.aux:
        problems.append(f"aux {sorted(m.aux)}: aux data is not in this slice (05); remove the "
                        "aux block and any plugin with required_aux")
    try:
        check_formats(list(m.outputs.formats))
    except (NotImplementedError, ValueError) as e:
        problems.append(str(e))

    # -- render names layers and their classes (07 section 3, 09 section 5)
    delivered = _delivered_names(m, problems)
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
