from __future__ import annotations

import ast
import io
import os
import re
import traceback
import uuid
from typing import Any, Dict, Optional, List, Tuple

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.element
import ifcopenshell.util.selector

import numpy as np

# -----------------------------------------------------------------------------
# IMPORTANT DESIGN CHANGE
# -----------------------------------------------------------------------------
# Do NOT serialize IFC models into JSON strings and pass them through the LLM.
# The model will corrupt/truncate them -> JSONDecodeError.
#
# Instead: keep IFC models in a server-side registry and pass a short handle_id.
#
# COMPATIBILITY NOTE
# -----------------------------------------------------------------------------
# Older agent/tool schemas may still send:
#   - "handle" instead of "handle_id"
#   - "return_handle" for ifc_python_exec
#
# This module accepts those legacy args as aliases to avoid hard crashes.
# -----------------------------------------------------------------------------

_IFC_REGISTRY: Dict[str, ifcopenshell.file] = {}


def _new_handle_id() -> str:
    return str(uuid.uuid4())


def _store_model(m: ifcopenshell.file) -> str:
    hid = _new_handle_id()
    _IFC_REGISTRY[hid] = m
    return hid


def _get_model(handle_id: str) -> ifcopenshell.file:
    try:
        return _IFC_REGISTRY[handle_id]
    except KeyError:
        raise ValueError(f"Unknown handle_id: {handle_id}")


def _set_model(handle_id: str, m: ifcopenshell.file) -> None:
    _IFC_REGISTRY[handle_id] = m


def _coalesce_handle_id(handle_id: Optional[str], handle: Optional[str]) -> str:
    """Accept legacy 'handle' as alias for 'handle_id'."""
    hid = handle_id or handle
    if not hid:
        raise TypeError("Missing handle_id (or legacy handle).")
    return hid


def _with_handle(payload: Dict[str, Any], handle_id: str) -> Dict[str, Any]:
    payload["handle_id"] = handle_id
    payload.setdefault("handle", handle_id)
    return payload


def _by_guid(m: ifcopenshell.file, guid: str):
    if hasattr(m, "by_guid"):
        try:
            e = m.by_guid(guid)
        except Exception:
            e = None
        if e:
            return e
    for e in m:
        if getattr(e, "GlobalId", None) == guid:
            return e
    raise ValueError(f"GUID not found: {guid}")


def _jsonify(x: Any) -> Any:
    # IfcOpenShell file (model) -> summary
    if isinstance(x, ifcopenshell.file):
        return {"_ifc_file": True, "schema": x.schema}

    # IFC entity -> guid/type/name
    if hasattr(x, "is_a") and hasattr(x, "GlobalId"):
        return {"_guid": x.GlobalId, "_type": x.is_a(), "_name": getattr(x, "Name", None)}

    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if isinstance(x, (list, tuple)):
        return [_jsonify(i) for i in x]
    if isinstance(x, dict):
        return {str(k): _jsonify(v) for k, v in x.items()}

    # Fallback: stringify
    return str(x)


def _resolve_guids(m: ifcopenshell.file, obj: Any) -> Any:
    if isinstance(obj, dict):
        # Convention: {"_guid":"..."} resolves to entity
        if "_guid" in obj and len(obj) == 1:
            return _by_guid(m, obj["_guid"])
        return {k: _resolve_guids(m, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_guids(m, v) for v in obj]
    return obj


# -------------------------
# Core file tools (registry-based)
# -------------------------

def ifc_new(schema: str = "IFC4") -> Dict[str, Any]:
    m = ifcopenshell.file(schema=schema)
    handle_id = _store_model(m)
    return _with_handle({"ok": True, "schema": schema}, handle_id)


def ifc_open(path: str) -> Dict[str, Any]:
    m = ifcopenshell.open(path)
    handle_id = _store_model(m)
    return _with_handle({"ok": True, "path": path, "schema": m.schema}, handle_id)


def ifc_save(handle_id: Optional[str] = None, handle: Optional[str] = None, path: str = "") -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    m.write(path)
    return _with_handle({"ok": True, "path": path}, hid)


def ifc_inspect(handle_id: Optional[str] = None, handle: Optional[str] = None) -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    counts = {}
    for t in (
        "IfcProject",
        "IfcSite",
        "IfcBuilding",
        "IfcBuildingStorey",
        "IfcSpace",
        "IfcWall",
        "IfcSlab",
        "IfcRoof",
        "IfcDoor",
        "IfcWindow",
    ):
        try:
            counts[t] = len(m.by_type(t))
        except Exception:
            pass
    return _with_handle({"ok": True, "schema": m.schema, "counts": counts}, hid)


# -------------------------
# Generic edit/query primitives
# -------------------------

def ifc_api(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    action: str = "",
    kwargs: Optional[Dict[str, Any]] = None,
    **extra: Any,  # accept unexpected args
) -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    merged: Dict[str, Any] = {}
    if kwargs:
        merged.update(kwargs)
    if extra:
        merged.update(extra)

    if (not action) and ("ifc_class" in merged):
        action = "root.create_entity"

    try:
        resolved = _resolve_guids(m, merged or {})
    except Exception as e:
        return _with_handle({"ok": False, "error": str(e), "action": action, "kwargs": _jsonify(merged)}, hid)

    try:
        result = ifcopenshell.api.run(action, m, **resolved)
    except Exception as e:
        return _with_handle({"ok": False, "error": str(e), "action": action, "kwargs": _jsonify(resolved)}, hid)

    return _with_handle({"ok": True, "action": action, "result": _jsonify(result)}, hid)


def ifc_get(handle_id: Optional[str] = None, handle: Optional[str] = None, guid: str = "") -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    try:
        e = _by_guid(m, guid)
    except ValueError as e:
        return _with_handle({"ok": False, "error": str(e), "guid": guid}, hid)
    info = e.get_info()
    safe = {k: _jsonify(v) for k, v in info.items()}
    return _with_handle({"ok": True, "guid": guid, "type": e.is_a(), "info": safe}, hid)


def ifc_set(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    guid: str = "",
    attrs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    try:
        e = _by_guid(m, guid)
    except ValueError as e:
        return _with_handle({"ok": False, "error": str(e), "guid": guid}, hid)
    try:
        for k, v in (attrs or {}).items():
            setattr(e, k, v)
    except Exception as ex:
        return _with_handle({"ok": False, "error": str(ex), "guid": guid}, hid)
    return _with_handle({"ok": True, "guid": guid}, hid)


def ifc_delete(handle_id: Optional[str] = None, handle: Optional[str] = None, guid: str = "") -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    try:
        e = _by_guid(m, guid)
    except ValueError as e:
        return _with_handle({"ok": False, "error": str(e), "guid": guid}, hid)
    m.remove(e)
    return _with_handle({"ok": True, "guid": guid}, hid)


def ifc_select(handle_id: Optional[str] = None, handle: Optional[str] = None, selector: str = "", limit: int = 500) -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    if not hasattr(ifcopenshell.util.selector, "parse"):
        # Fallback: allow simple type selectors like "IfcWall"
        sel = (selector or "").strip()
        if re.match(r"^Ifc[A-Za-z0-9_]+$", sel):
            items = m.by_type(sel) or []
            if limit and len(items) > limit:
                items = items[:limit]
            out = []
            for e in items:
                out.append(
                    {
                        "guid": getattr(e, "GlobalId", None),
                        "type": e.is_a(),
                        "name": getattr(e, "Name", None),
                    }
                )
            return _with_handle({"ok": True, "selector": selector, "count": len(out), "items": out}, hid)

        return {
            "ok": False,
            "error": "ifcopenshell.util.selector.parse is not available; only simple type selectors like 'IfcWall' are supported in this build",
            "handle_id": hid,
        }

    items = ifcopenshell.util.selector.parse(m, selector) or []
    if limit and len(items) > limit:
        items = items[:limit]

    out = []
    for e in items:
        out.append(
            {
                "guid": getattr(e, "GlobalId", None),
                "type": e.is_a(),
                "name": getattr(e, "Name", None),
            }
        )
    return _with_handle({"ok": True, "selector": selector, "count": len(out), "items": out}, hid)


def ifc_get_psets(handle_id: Optional[str] = None, handle: Optional[str] = None, guid: str = "") -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    try:
        e = _by_guid(m, guid)
    except ValueError as e:
        return _with_handle({"ok": False, "error": str(e), "guid": guid}, hid)
    psets = ifcopenshell.util.element.get_psets(e) or {}
    return _with_handle({"ok": True, "guid": guid, "psets": psets}, hid)


def ifc_get_materials(handle_id: Optional[str] = None, handle: Optional[str] = None, guid: str = "") -> Dict[str, Any]:
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    try:
        e = _by_guid(m, guid)
    except ValueError as e:
        return _with_handle({"ok": False, "error": str(e), "guid": guid}, hid)
    mats = ifcopenshell.util.element.get_materials(e) or []
    names = sorted({getattr(mm, "Name", None) for mm in mats if getattr(mm, "Name", None)})
    return _with_handle({"ok": True, "guid": guid, "type": e.is_a(), "materials": names}, hid)


# -----------------------------------------------------------------------------
# Viewable geometry primitives (NEW)
# -----------------------------------------------------------------------------

def _first_or_none(m: ifcopenshell.file, ifc_class: str):
    try:
        items = m.by_type(ifc_class)
        return items[0] if items else None
    except Exception:
        return None


def _ensure_units_meters(m: ifcopenshell.file) -> None:
    """
    Ensure project has a basic SI length unit assigned (METRE).
    Uses unit.add_si_unit + unit.assign_unit.  [oai_citation:7‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/unit/add_si_unit/index.html?utm_source=chatgpt.com)
    """
    # If units already assigned, do nothing.
    project = _first_or_none(m, "IfcProject")
    if project and getattr(project, "UnitsInContext", None) and getattr(project.UnitsInContext, "Units", None):
        if project.UnitsInContext.Units:
            return

    # API signature differs across IfcOpenShell versions.
    try:
        lengthunit = ifcopenshell.api.run("unit.add_si_unit", m, unit_type="LENGTHUNIT", name="METRE")
    except TypeError:
        lengthunit = ifcopenshell.api.run("unit.add_si_unit", m, unit_type="LENGTHUNIT")
    ifcopenshell.api.run("unit.assign_unit", m, units=[lengthunit])


def _ensure_contexts(m: ifcopenshell.file):
    """
    Ensure Model context and Body subcontext exist.  [oai_citation:8‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/context/add_context/index.html?utm_source=chatgpt.com)
    Returns: (model_context, body_context)
    """
    model_ctx = None
    body_ctx = None
    try:
        # Many builds allow querying contexts through the file.
        for ctx in (m.by_type("IfcGeometricRepresentationContext") or []):
            if getattr(ctx, "ContextType", None) == "Model":
                model_ctx = ctx
                break
    except Exception:
        model_ctx = None

    if model_ctx is None:
        model_ctx = ifcopenshell.api.run("context.add_context", m, context_type="Model")

    # Body is a subcontext (IfcGeometricRepresentationSubContext)
    try:
        for ctx in (m.by_type("IfcGeometricRepresentationSubContext") or []):
            if getattr(ctx, "ContextIdentifier", None) == "Body" and getattr(ctx, "TargetView", None) == "MODEL_VIEW":
                body_ctx = ctx
                break
    except Exception:
        body_ctx = None

    if body_ctx is None:
        body_ctx = ifcopenshell.api.run(
            "context.add_context",
            m,
            context_type="Model",
            context_identifier="Body",
            target_view="MODEL_VIEW",
            parent=model_ctx,
        )

    return model_ctx, body_ctx


def _ensure_spatial_tree(m: ifcopenshell.file, project_name: str, site_name: str, building_name: str):
    """
    Ensure Project -> Site -> Building exist and are aggregated.  [oai_citation:9‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/aggregate/assign_object/index.html?utm_source=chatgpt.com)
    Returns: (project, site, building)
    """
    project = _first_or_none(m, "IfcProject")
    if project is None:
        project = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcProject", name=project_name)

    site = _first_or_none(m, "IfcSite")
    if site is None:
        site = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcSite", name=site_name)

    building = _first_or_none(m, "IfcBuilding")
    if building is None:
        building = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcBuilding", name=building_name)

    # Aggregate: Project contains Site; Site contains Building
    # assign_object signature: (file, products=[...], relating_object=...)
    ifcopenshell.api.run("aggregate.assign_object", m, products=[site], relating_object=project)
    ifcopenshell.api.run("aggregate.assign_object", m, products=[building], relating_object=site)

    # Give base placements (identity) so downstream relative placements behave sanely
    ident = np.eye(4, dtype=float)
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=site, matrix=ident, is_si=True)
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=building, matrix=ident, is_si=True)

    return project, site, building


def ifc_ensure_project_setup(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    project_name: str = "Project",
    site_name: str = "Site",
    building_name: str = "Building",
    ensure_default_storey: bool = True,
    default_storey_name: str = "Level 0",
    default_storey_elevation_m: float = 0.0,
) -> Dict[str, Any]:
    """
    Idempotent: ensures minimum viable, viewable IFC foundations:
      - IfcProject + meters unit assignment
      - Model + Body context
      - Project->Site->Building aggregation
      - (optional) at least one storey

    Returns GUIDs + a hint about Body context (not GUID-based).
    """
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    _ensure_units_meters(m)
    model_ctx, body_ctx = _ensure_contexts(m)
    project, site, building = _ensure_spatial_tree(m, project_name, site_name, building_name)

    storeys: List[str] = []
    if ensure_default_storey:
        existing = []
        try:
            existing = m.by_type("IfcBuildingStorey") or []
        except Exception:
            existing = []
        if existing:
            storeys = [getattr(s, "GlobalId", None) for s in existing if getattr(s, "GlobalId", None)]
        else:
            st = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcBuildingStorey", name=default_storey_name)
            # aggregate under building
            ifcopenshell.api.run("aggregate.assign_object", m, products=[st], relating_object=building)
            # placement at elevation
            mat = np.eye(4, dtype=float)
            mat[2, 3] = float(default_storey_elevation_m)
            ifcopenshell.api.run("geometry.edit_object_placement", m, product=st, matrix=mat, is_si=True)
            storeys = [st.GlobalId]

    payload = {
        "ok": True,
        "project_guid": getattr(project, "GlobalId", None),
        "site_guid": getattr(site, "GlobalId", None),
        "building_guid": getattr(building, "GlobalId", None),
        "storey_guids": storeys,
        # Entity instances in API are not GUID-addressable; return minimal info
        "body_context": {"type": body_ctx.is_a(), "ContextIdentifier": getattr(body_ctx, "ContextIdentifier", None), "TargetView": getattr(body_ctx, "TargetView", None)},
    }
    _set_model(hid, m)
    return _with_handle(payload, hid)


def ifc_add_storey(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    building_guid: str = "",
    name: str = "Level",
    elevation_m: float = 0.0,
) -> Dict[str, Any]:
    """
    Create an IfcBuildingStorey under the given building GUID, and place it at Z=elevation_m.
    """
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    building = _by_guid(m, building_guid)
    st = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcBuildingStorey", name=name)
    ifcopenshell.api.run("aggregate.assign_object", m, products=[st], relating_object=building)

    mat = np.eye(4, dtype=float)
    mat[2, 3] = float(elevation_m)
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=st, matrix=mat, is_si=True)

    _set_model(hid, m)
    return _with_handle({"ok": True, "storey_guid": st.GlobalId, "name": name, "elevation_m": elevation_m}, hid)


def ifc_set_local_placement(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    element_guid: str = "",
    matrix_4x4: Any = None,
    should_transform_children: bool = False,
) -> Dict[str, Any]:
    """
    Set ObjectPlacement via geometry.edit_object_placement (expects a 4x4 numpy matrix).  [oai_citation:10‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/geometry/edit_object_placement/index.html)
    """
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)
    el = _by_guid(m, element_guid)

    if matrix_4x4 is None:
        mat = np.eye(4, dtype=float)
    else:
        mat = np.array(matrix_4x4, dtype=float)
        if mat.shape != (4, 4):
            return _with_handle({"ok": False, "error": f"matrix_4x4 must be 4x4, got shape {mat.shape}"}, hid)

    lp = ifcopenshell.api.run(
        "geometry.edit_object_placement",
        m,
        product=el,
        matrix=mat,
        is_si=True,
        should_transform_children=bool(should_transform_children),
    )

    _set_model(hid, m)
    return _with_handle({"ok": True, "element_guid": element_guid, "placement_entity": _jsonify(lp)}, hid)


def _z_rotation_matrix(theta: float) -> np.ndarray:
    c = float(np.cos(theta))
    s = float(np.sin(theta))
    mat = np.eye(4, dtype=float)
    mat[0, 0] = c
    mat[0, 1] = -s
    mat[1, 0] = s
    mat[1, 1] = c
    return mat


def _placement_from_xy_theta(x: float, y: float, z: float, theta: float) -> np.ndarray:
    mat = _z_rotation_matrix(theta)
    mat[0, 3] = float(x)
    mat[1, 3] = float(y)
    mat[2, 3] = float(z)
    return mat


def ifc_add_wall(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    storey_guid: str = "",
    start_xy: List[float] = None,
    end_xy: List[float] = None,
    height_m: float = 3.0,
    thickness_m: float = 0.2,
    base_z_m: float = 0.0,
    name: str = "Wall",
    centerline: bool = True,
) -> Dict[str, Any]:
    """
    Create a viewable IfcWall:
      - creates entity
      - assigns to storey (containment)  [oai_citation:11‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/spatial/assign_container/index.html?utm_source=chatgpt.com)
      - sets placement  [oai_citation:12‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/geometry/edit_object_placement/index.html)
      - creates Body representation (parametric)  [oai_citation:13‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/geometry/add_wall_representation/index.html)
      - assigns representation  [oai_citation:14‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/geometry/assign_representation/index.html?utm_source=chatgpt.com)
    """
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    if not start_xy or not end_xy or len(start_xy) != 2 or len(end_xy) != 2:
        return _with_handle({"ok": False, "error": "start_xy and end_xy must be [x,y]"}, hid)

    _ensure_units_meters(m)
    _, body_ctx = _ensure_contexts(m)

    storey = _by_guid(m, storey_guid)

    x0, y0 = float(start_xy[0]), float(start_xy[1])
    x1, y1 = float(end_xy[0]), float(end_xy[1])
    dx, dy = x1 - x0, y1 - y0
    length = float(np.hypot(dx, dy))
    if length <= 1e-6:
        return _with_handle({"ok": False, "error": "Wall length is too small"}, hid)

    theta = float(np.arctan2(dy, dx))

    wall = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcWall", name=name)

    # Containment in storey
    ifcopenshell.api.run("spatial.assign_container", m, products=[wall], relating_structure=storey)

    # Placement at start point, rotated along wall direction
    mat = _placement_from_xy_theta(x0, y0, float(base_z_m), theta)
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=wall, matrix=mat, is_si=True)

    # Representation (axis-aligned in local coords)
    offset = (-float(thickness_m) / 2.0) if centerline else 0.0
    rep = ifcopenshell.api.run(
        "geometry.add_wall_representation",
        m,
        context=body_ctx,
        length=float(length),
        height=float(height_m),
        thickness=float(thickness_m),
        offset=float(offset),
    )
    ifcopenshell.api.run("geometry.assign_representation", m, product=wall, representation=rep)

    _set_model(hid, m)
    return _with_handle(
        {
            "ok": True,
            "wall_guid": wall.GlobalId,
            "name": name,
            "start_xy": [x0, y0],
            "end_xy": [x1, y1],
            "length_m": length,
            "height_m": float(height_m),
            "thickness_m": float(thickness_m),
            "base_z_m": float(base_z_m),
        },
        hid,
    )


def _closed_polyline(poly: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not poly:
        return poly
    if poly[0] != poly[-1]:
        return poly + [poly[0]]
    return poly


def ifc_add_slab(
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    storey_guid: str = "",
    polyline_xy: List[List[float]] = None,
    depth_m: float = 0.2,
    z_m: float = 0.0,
    name: str = "Slab",
    predefined_type: str = "FLOOR",
) -> Dict[str, Any]:
    """
    Create a viewable IfcSlab from a 2D polyline:
      - polyline_xy: list of [x,y] points (will be closed if not already)
      - creates entity
      - containment in storey  [oai_citation:15‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/spatial/assign_container/index.html?utm_source=chatgpt.com)
      - placement + slab representation with polyline  [oai_citation:16‡IfcOpenShell Dokumentation](https://docs.ifcopenshell.org/autoapi/ifcopenshell/api/geometry/add_slab_representation/index.html)
    """
    hid = _coalesce_handle_id(handle_id, handle)
    m = _get_model(hid)

    if not polyline_xy or len(polyline_xy) < 3:
        return _with_handle({"ok": False, "error": "polyline_xy must have at least 3 points"}, hid)

    pts_world: List[Tuple[float, float]] = [(float(p[0]), float(p[1])) for p in polyline_xy]
    pts_world = _closed_polyline(pts_world)

    xs = [p[0] for p in pts_world]
    ys = [p[1] for p in pts_world]
    minx, miny = min(xs), min(ys)

    # Localize polyline near origin for numerical stability
    pts_local = [(p[0] - minx, p[1] - miny) for p in pts_world]

    _ensure_units_meters(m)
    _, body_ctx = _ensure_contexts(m)

    storey = _by_guid(m, storey_guid)

    slab = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcSlab", predefined_type=predefined_type, name=name)
    ifcopenshell.api.run("spatial.assign_container", m, products=[slab], relating_structure=storey)

    mat = np.eye(4, dtype=float)
    mat[0, 3] = float(minx)
    mat[1, 3] = float(miny)
    mat[2, 3] = float(z_m)
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=slab, matrix=mat, is_si=True)

    rep = ifcopenshell.api.run(
        "geometry.add_slab_representation",
        m,
        context=body_ctx,
        depth=float(depth_m),
        polyline=pts_local,
    )
    ifcopenshell.api.run("geometry.assign_representation", m, product=slab, representation=rep)

    _set_model(hid, m)
    return _with_handle(
        {
            "ok": True,
            "slab_guid": slab.GlobalId,
            "name": name,
            "predefined_type": predefined_type,
            "depth_m": float(depth_m),
            "z_m": float(z_m),
            "polyline_xy": [[p[0], p[1]] for p in pts_world],
            "placement_origin_xy": [float(minx), float(miny)],
        },
        hid,
    )


# -------------------------
# IfcOpenShell Python execution tool (registry-based)
# -------------------------

def ifc_python_exec(
    *,
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,          # legacy alias
    ifc_path: Optional[str] = None,
    code: str,
    return_handle: Optional[bool] = None,  # legacy arg; ignored
) -> Dict[str, Any]:
    """
    Execute IfcOpenShell Python code for generation and retrieval.

    Provide either:
      - handle_id (preferred), or legacy handle (alias), or
      - ifc_path (loads into a NEW handle_id), or
      - neither (creates a NEW IFC4 model handle_id)

    Environment:
      - model / ifc: ifcopenshell.file
      - ifc_path: string path if provided to the tool, else None
      - api: ifcopenshell.api (module)
      - selector: ifcopenshell.util.selector (module; may have parse())
      - util: ifcopenshell.util.element
      - result: set this to JSON-serializable output

    Safety:
      - Strips import lines automatically (LLMs often add them).
      - Blocks imports and dunder attribute access.
      - Not a perfect sandbox; treat as dev tooling unless OS-sandboxed.
    """
    if handle_id is None and handle is not None:
        handle_id = handle

    if handle_id:
        m = _get_model(handle_id)
        hid = handle_id
    elif ifc_path:
        m = ifcopenshell.open(ifc_path)
        hid = _store_model(m)
    else:
        m = ifcopenshell.file(schema="IFC4")
        hid = _store_model(m)

    code = "\n".join(
        line for line in code.splitlines()
        if not line.lstrip().startswith("import ")
        and not line.lstrip().startswith("from ")
    )

    _basic_safety_check(code)

    stdout = io.StringIO()

    safe_globals = {
        "__builtins__": {
            "True": True,
            "False": False,
            "None": None,
            "bool": bool,
            "str": str,
            "int": int,
            "float": float,
            "len": len,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "any": any,
            "all": all,
            "range": range,
            "enumerate": enumerate,
            "sorted": sorted,
            "zip": zip,
            "list": list,
            "dict": dict,
            "set": set,
            "tuple": tuple,
            "hasattr": hasattr,
            "getattr": getattr,
            "isinstance": isinstance,
            "Exception": Exception,
            "print": lambda *a, **k: print(*a, file=stdout, **k),
        }
    }
    safe_locals = {
        "ifcopenshell": ifcopenshell,
        "api": ifcopenshell.api,
        "util": ifcopenshell.util.element,
        "selector": ifcopenshell.util.selector,
        "model": m,
        "ifc": m,  # legacy alias expected by some prompts
        "ifc_path": ifc_path,
        "result": None,
    }

    try:
        exec(compile(code, "<ifc_python_exec>", "exec"), safe_globals, safe_locals)
        payload = {
            "ok": True,
            "stdout": stdout.getvalue(),
            "result": safe_locals.get("result", None),
            "handle_id": hid,
        }
    except Exception:
        payload = {
            "ok": False,
            "stdout": stdout.getvalue(),
            "error": traceback.format_exc(),
            "handle_id": hid,
        }

    _set_model(hid, m)
    return _with_handle(payload, hid)


def ifc_python_query(
    *,
    handle_id: Optional[str] = None,
    handle: Optional[str] = None,
    ifc_path: Optional[str] = None,
    code: str = "",
) -> Dict[str, Any]:
    """
    Read-only convenience wrapper (best-effort).
    NOTE: This does not prevent mutation; it's only a semantic hint.
    """
    return ifc_python_exec(handle_id=handle_id, handle=handle, ifc_path=ifc_path, code=code, return_handle=False)


def _basic_safety_check(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("Imports are not allowed in ifc_python_exec()")
        if isinstance(node, ast.Attribute) and isinstance(node.attr, str) and node.attr.startswith("__"):
            raise ValueError("Dunder attribute access is not allowed in ifc_python_exec()")
