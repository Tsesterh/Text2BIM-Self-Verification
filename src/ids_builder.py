from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ifctester import facet as facet_mod
from ifctester import ids as ids_mod


# -----------------------------------------------------------------------------
# Build IDS via IfcTester objects ONLY (no hand-written XML).
# Add sanitizers so the plan can never create schema-invalid values.
# -----------------------------------------------------------------------------


def _norm_key(k: str) -> str:
    if k.startswith("@"):
        k = k[1:]
    return k


def _coerce_facet_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    if not kwargs:
        return {}
    out: Dict[str, Any] = {}
    for k, v in kwargs.items():
        out[_norm_key(str(k))] = v
    return out


def _is_facet_class(obj: Any) -> bool:
    try:
        return inspect.isclass(obj) and issubclass(obj, facet_mod.Facet)
    except Exception:
        return False


def list_available_facets() -> List[str]:
    names: List[str] = []
    for name in dir(facet_mod):
        if name.startswith("_"):
            continue
        obj = getattr(facet_mod, name)
        if _is_facet_class(obj):
            names.append(name)
    names.sort()
    return names


def facet_signature(facet_type: str) -> Dict[str, Any]:
    cls = getattr(facet_mod, facet_type, None)
    if not _is_facet_class(cls):
        raise ValueError(f"Unknown/unsupported facet_type: {facet_type}")

    sig = inspect.signature(cls.__init__)
    params = []
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        params.append(
            {
                "name": p.name,
                "kind": str(p.kind),
                "required": p.default is inspect._empty,
                "default": None if p.default is inspect._empty else p.default,
            }
        )
    return {"facet_type": facet_type, "params": params, "doc": (cls.__doc__ or "").strip()}


# ---- IDS enum sanitizers -----------------------------------------------------

_ALLOWED_PARTOF_RELATIONS = {
    "IFCRELAGGREGATES",
    "IFCRELASSIGNSTOGROUP",
    "IFCRELCONTAINEDINSPATIALSTRUCTURE",
    "IFCRELNESTS",
    # yes, your schema shows this odd combined token as one enum item:
    "IFCRELVOIDSELEMENT IFCRELFILLSELEMENT",
}


def _normalize_relation_enum(val: Any) -> Optional[str]:
    """
    Normalize relation strings to the IDS enum form used by your IfcTester schema.

    Examples:
      - "IfcRelContainedInSpatialStructure" -> "IFCRELCONTAINEDINSPATIALSTRUCTURE"
      - "IFCRELCONTAINEDINSPATIALSTRUCTURE" -> unchanged

    Returns:
      - normalized string if valid
      - None if cannot be normalized into allowed set
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None

    # Already valid?
    if s in _ALLOWED_PARTOF_RELATIONS:
        return s

    # Common LLM form: "IfcRelContainedInSpatialStructure"
    # Convert by stripping leading "Ifc" and uppercasing
    if s.startswith("Ifc"):
        s2 = s[3:].upper()
        if s2 in _ALLOWED_PARTOF_RELATIONS:
            return s2

    # Another common: "IfcRelContainedInSpatialStructure".upper() still has "IFCREL..."
    s3 = s.upper()
    if s3 in _ALLOWED_PARTOF_RELATIONS:
        return s3

    # Some people might pass "IfcRelContainedInSpatialStructure" with spaces/underscores etc.
    s4 = re.sub(r"[^A-Z ]+", "", s3)  # keep only A-Z and spaces
    s4 = re.sub(r"\s+", " ", s4).strip()
    if s4 in _ALLOWED_PARTOF_RELATIONS:
        return s4

    return None


def _sanitize_facet_kwargs(facet_type: str, kw: Dict[str, Any], warnings: List[str], spec_name: str) -> Optional[Dict[str, Any]]:
    """
    Returns possibly modified kw, or None to indicate "drop this facet".
    """
    # PartOf facet: relation must be one of the schema enums.
    if facet_type == "PartOf" and "relation" in kw:
        norm = _normalize_relation_enum(kw.get("relation"))
        if norm is None:
            warnings.append(
                f"Spec '{spec_name}': dropped PartOf facet because relation='{kw.get('relation')}' "
                f"is not one of allowed {_ALLOWED_PARTOF_RELATIONS}"
            )
            return None
        kw["relation"] = norm
    return kw


def _make_facet(facet_type: str, args: Dict[str, Any], warnings: List[str], spec_name: str):
    cls = getattr(facet_mod, facet_type, None)
    if not _is_facet_class(cls):
        raise ValueError(f"Unknown/unsupported facet_type: {facet_type}")

    kw = _coerce_facet_kwargs(args or {})

    # Validate keys against constructor signature
    sig = inspect.signature(cls.__init__)
    allowed = {p.name for p in sig.parameters.values() if p.name != "self"}
    unknown = sorted(set(kw.keys()) - allowed)
    if unknown:
        raise ValueError(f"{facet_type}: unknown args {unknown}. Allowed: {sorted(allowed)}")

    # Sanitize enum-ish args to avoid schema errors
    kw2 = _sanitize_facet_kwargs(facet_type, kw, warnings, spec_name)
    if kw2 is None:
        return None

    return cls(**kw2)


# ---- Plan model --------------------------------------------------------------

@dataclass
class IdsSpecPlan:
    name: str
    ifcVersion: List[str] = field(default_factory=lambda: ["IFC4"])
    identifier: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    minOccurs: int = 0
    maxOccurs: Any = "unbounded"
    applicability: List[Dict[str, Any]] = field(default_factory=list)
    requirements: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class IdsPlan:
    title: str = "Generated IDS"
    description: Optional[str] = None
    version: Optional[str] = "1.0"
    author: Optional[str] = None
    date: Optional[str] = None
    specifications: List[IdsSpecPlan] = field(default_factory=list)


def build_ids_from_plan(plan: Dict[str, Any]) -> Tuple[ids_mod.Ids, List[str]]:
    warnings: List[str] = []

    title = str(plan.get("title") or "Generated IDS")
    description = plan.get("description")
    version = plan.get("version")
    author = plan.get("author")
    date = plan.get("date")

    ids_obj = ids_mod.Ids(
        title=title,
        description=description,
        version=version,
        author=author,
        date=date,
    )

    specs_raw = plan.get("specifications") or []
    if not isinstance(specs_raw, list):
        raise ValueError("plan.specifications must be a list")

    for s in specs_raw:
        if not isinstance(s, dict):
            raise ValueError("Each specification must be an object")

        name = s.get("name")
        if not name:
            raise ValueError("Each specification must have a name")
        spec_name = str(name)

        ifc_version = s.get("ifcVersion") or ["IFC4"]
        if isinstance(ifc_version, str):
            ifc_version = [ifc_version]
        if not isinstance(ifc_version, list) or not ifc_version:
            ifc_version = ["IFC4"]

        spec = ids_mod.Specification(
            name=spec_name,
            ifcVersion=ifc_version,
            identifier=s.get("identifier"),
            description=s.get("description"),
            instructions=s.get("instructions"),
            minOccurs=int(s.get("minOccurs", 0)),
            maxOccurs=s.get("maxOccurs", "unbounded"),
        )

        # Applicability facets
        for f in (s.get("applicability") or []):
            if not isinstance(f, dict):
                raise ValueError(f"Spec {spec_name}: applicability facet must be object")
            facet_type = f.get("facet_type") or f.get("type")
            args = f.get("args") or {}
            if not facet_type:
                raise ValueError(f"Spec {spec_name}: facet missing facet_type")
            facet_obj = _make_facet(str(facet_type), args, warnings, spec_name)
            if facet_obj is not None:
                spec.applicability.append(facet_obj)

        # Requirement facets
        for f in (s.get("requirements") or []):
            if not isinstance(f, dict):
                raise ValueError(f"Spec {spec_name}: requirement facet must be object")
            facet_type = f.get("facet_type") or f.get("type")
            args = f.get("args") or {}
            if not facet_type:
                raise ValueError(f"Spec {spec_name}: facet missing facet_type")
            facet_obj = _make_facet(str(facet_type), args, warnings, spec_name)
            if facet_obj is not None:
                spec.requirements.append(facet_obj)

        if not spec.applicability:
            warnings.append(f"Spec '{spec_name}': no applicability facets remain after sanitization.")
        if not spec.requirements:
            warnings.append(f"Spec '{spec_name}': no requirement facets remain after sanitization.")

        # Only append specs that still have both sides meaningful
        if spec.applicability and spec.requirements:
            ids_obj.specifications.append(spec)
        else:
            warnings.append(f"Spec '{spec_name}': dropped (missing applicability/requirements).")

    return ids_obj, warnings


def validate_ids_object(ids_obj: ids_mod.Ids) -> Tuple[bool, Optional[str]]:
    xml = ids_obj.to_string()
    try:
        ids_mod.from_string(xml, validate=True)
        return True, None
    except Exception as e:
        return False, str(e)


def write_ids_from_plan(plan: Dict[str, Any], out_path: str) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    ids_obj, warnings = build_ids_from_plan(plan)
    ok, err = validate_ids_object(ids_obj)
    if not ok:
        raise ValueError(f"IDS schema validation failed: {err}")

    xml = ids_obj.to_string()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)

    return {"ok": True, "ids_path": out_path, "warnings": warnings}


# ---- LLM helper tools --------------------------------------------------------

def ids_facet_list() -> Dict[str, Any]:
    return {"ok": True, "facets": list_available_facets()}


def ids_facet_schema(facet_type: str) -> Dict[str, Any]:
    return {"ok": True, "schema": facet_signature(facet_type)}


def ids_plan_validate(plan: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ids_obj, warnings = build_ids_from_plan(plan)
        ok, err = validate_ids_object(ids_obj)
        return {"ok": ok, "warnings": warnings, "error": err}
    except Exception as e:
        return {"ok": False, "warnings": [], "error": str(e)}