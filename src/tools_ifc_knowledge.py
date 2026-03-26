from __future__ import annotations

from typing import Any, Dict, List, Optional

import ifcopenshell
import ifcopenshell.util.doc
import ifcopenshell.util.schema


def _schema_from_str(name: str) -> str:
    return (name or "IFC4").upper()


def ifc_schema_entity_exists(*, entity: str, schema: str = "IFC4") -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        sd = ifcopenshell.util.doc.get_schema_by_name(schema)
        if sd is None:
            return {"ok": False, "error": f"Schema not available: {schema}", "entity": entity, "schema": schema}
        decl = sd.declaration_by_name(entity)
        return {"ok": True, "entity": entity, "schema": schema, "exists": decl is not None}
    except Exception as e:
        return {"ok": False, "error": str(e), "entity": entity, "schema": schema}


def ifc_schema_subtypes(*, base_entity: str, schema: str = "IFC4") -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        sd = ifcopenshell.util.doc.get_schema_by_name(schema)
        if sd is None:
            return {"ok": False, "error": f"Schema not available: {schema}", "base_entity": base_entity, "schema": schema}
        decl = sd.declaration_by_name(base_entity)
        if decl is None:
            return {"ok": False, "error": f"Unknown entity: {base_entity}", "base_entity": base_entity, "schema": schema}

        subs: List[str] = []
        try:
            subs = list(ifcopenshell.util.schema.get_subtypes(decl))  # type: ignore
        except Exception:
            for d in sd.declarations():
                try:
                    if d.is_subtype_of(decl):
                        subs.append(d.name())
                except Exception:
                    pass

        subs = sorted(set(str(s) for s in subs if s))
        return {"ok": True, "schema": schema, "base_entity": base_entity, "subtypes": subs}
    except Exception as e:
        return {"ok": False, "error": str(e), "schema": schema, "base_entity": base_entity}


def ifc_doc_entity(*, entity: str, schema: str = "IFC4", recursive: bool = True) -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        doc = ifcopenshell.util.doc.get_entity_doc(schema, entity, recursive=recursive)
        if doc is None:
            return {"ok": False, "error": f"No documentation found for entity {entity} in {schema}", "entity": entity, "schema": schema}

        out: Dict[str, Any] = {
            "ok": True,
            "schema": schema,
            "entity": entity,
            "description": getattr(doc, "description", None),
        }
        attrs = getattr(doc, "attributes", None)
        if isinstance(attrs, dict):
            out["attributes"] = sorted(list(attrs.keys()))
        else:
            out["attributes"] = None
        return out
    except Exception as e:
        return {"ok": False, "error": str(e), "entity": entity, "schema": schema}


def ifc_doc_type(*, ifc_type: str, schema: str = "IFC4") -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        doc = ifcopenshell.util.doc.get_type_doc(schema, ifc_type)
        if doc is None:
            return {"ok": False, "error": f"No type doc found for {ifc_type} in {schema}", "ifc_type": ifc_type, "schema": schema}
        return {"ok": True, "schema": schema, "ifc_type": ifc_type, "description": getattr(doc, "description", None)}
    except Exception as e:
        return {"ok": False, "error": str(e), "ifc_type": ifc_type, "schema": schema}


def ifc_doc_predefined_type(*, entity: str, predefined_type: str, schema: str = "IFC4") -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        txt = ifcopenshell.util.doc.get_predefined_type_doc(schema, entity, predefined_type)
        if txt is None:
            return {"ok": False, "error": "No predefined type doc found", "schema": schema, "entity": entity, "predefined_type": predefined_type}
        return {"ok": True, "schema": schema, "entity": entity, "predefined_type": predefined_type, "description": txt}
    except Exception as e:
        return {"ok": False, "error": str(e), "schema": schema, "entity": entity, "predefined_type": predefined_type}


def ifc_doc_property_set(*, pset: str, schema: str = "IFC4") -> Dict[str, Any]:
    schema = _schema_from_str(schema)
    try:
        doc = ifcopenshell.util.doc.get_property_set_doc(schema, pset)
        if doc is None:
            return {"ok": False, "error": f"No doc found for {pset}", "schema": schema, "pset": pset}
        props = getattr(doc, "properties", None)
        prop_names = sorted(list(props.keys())) if isinstance(props, dict) else None
        return {"ok": True, "schema": schema, "pset": pset, "description": getattr(doc, "description", None), "properties": prop_names}
    except Exception as e:
        return {"ok": False, "error": str(e), "schema": schema, "pset": pset}


def ifc_selector_syntax_help() -> Dict[str, Any]:
    return {
        "ok": True,
        "notes": [
            "IfcOpenShell selector syntax examples (depends on your build):",
            "- 'IfcSpace' -> all IfcSpace",
            "- 'IfcWall[Name*=External]' -> walls with Name containing 'External'",
            "- 'IfcBuildingStorey[Elevation>0]' -> storeys above 0",
        ],
    }