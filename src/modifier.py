from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from llm import LLMRunner
import tools_ifc


MODIFIER_INSTRUCTIONS = """You are the Modifier agent: a general IFC editor.

Goal:
- Produce or update an IFC model to satisfy the patch plan and (implicitly) the spec.
- The IFC MUST be viewable in a typical IFC viewer.
- If ask to construct buildings, ensure physical elements (walls, slabs, roofs, etc.) exist with geometry and placement.
- If asked to construct buildings, ensure they are realistic, i.e., have doors, windows, roof, etc. 

Viewability rules (non-negotiable):
- Ensure the project has a geometric representation context (Model + Body).
- Any physical element you create (walls, slabs, roofs, doors, windows, etc.) must have:
  - ObjectPlacement set, AND
  - a Body representation assigned (geometry).
- Prefer the dedicated geometry primitives (ifc_add_wall, ifc_add_slab) for viewable building structure.

You have access to IFC tools:
- ifc_new / ifc_open / ifc_save
- ifc_api: run any ifcopenshell.api usecase (preferred for standard operations)
- ifc_get / ifc_set / ifc_delete
- ifc_select
- ifc_python_exec: execute IfcOpenShell Python snippets (set `result` for structured output)

And viewability primitives:
- ifc_ensure_project_setup (creates/ensures project + units + contexts + site/building/storey)
- ifc_add_storey
- ifc_set_local_placement
- ifc_add_wall
- ifc_add_slab

IMPORTANT:
- When generating code for ifc_python_exec, DO NOT use import statements.
- The execution environment already provides: model/ifc, ifc_path, ifcopenshell, api, selector, util.
- Basic builtins like str/int/len/hasattr/getattr are available.

Rules:
- Work on the provided `handle` (latest IFC state). Tool results may include an updated handle.
- Prefer ifc_api for common actions; use ifc_python_exec for complex edits or bulk operations.
- If patch_plan items reference requirement IDs, mention them in your change log.
- If patch_plan is empty, build a minimal model that satisfies the user_prompt.
- If patch_plan is non-empty, focus only on listed items; avoid unrelated edits.
- Always ensure the final IFC is saved to output_ifc_path.

Output:
- A concise change log (plain text) describing what you did.
"""


def modifier_tools_schema() -> List[Dict[str, Any]]:
    def fn(name, desc, params, req):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": params, "required": req},
            },
        }

    # Reusable JSON-schema fragments
    number = {"type": "number"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    string = {"type": "string"}

    vec2 = {"type": "array", "items": number, "minItems": 2, "maxItems": 2}
    polyline2 = {"type": "array", "items": vec2, "minItems": 3}
    matrix_4x4 = {
        "type": "array",
        "items": {"type": "array", "items": number, "minItems": 4, "maxItems": 4},
        "minItems": 4,
        "maxItems": 4,
    }

    return [
        fn("ifc_new", "Create a new empty IFC model.", {"schema": string}, []),
        fn("ifc_open", "Open IFC from disk.", {"path": string}, ["path"]),
        fn("ifc_save", "Save IFC handle to disk path.", {"handle": string, "path": string}, ["handle", "path"]),
        fn("ifc_to_spf", "Export the handle to SPF string.", {"handle": string}, ["handle"]),
        fn("ifc_inspect", "Get schema and basic entity counts.", {"handle": string}, ["handle"]),
        fn(
            "ifc_api",
            "Run ifcopenshell.api.run(action, model, **kwargs).",
            {"handle": string, "action": string, "kwargs": {"type": "object"}},
            ["handle", "action", "kwargs"],
        ),
        fn("ifc_get", "Get entity info by GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn("ifc_set", "Set attributes on entity by GUID.", {"handle": string, "guid": string, "attrs": {"type": "object"}}, ["handle", "guid", "attrs"]),
        fn("ifc_delete", "Delete entity by GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn("ifc_select", "Select entities using selector syntax.", {"handle": string, "selector": string, "limit": integer}, ["handle", "selector"]),
        fn(
            "ifc_python_exec",
            "Execute IfcOpenShell python code (set `result`). Can create/modify/query. No imports; available locals: model/ifc, ifc_path, ifcopenshell, api, util, selector. Basic builtins like str/int/len/hasattr are available.",
            {"handle": string, "ifc_path": string, "code": string, "return_handle": boolean},
            ["code"],
        ),
        fn("ifc_get_psets", "Get all property sets for GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn("ifc_get_materials", "Get materials for GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),

        # -------------------------
        # Viewability primitives
        # -------------------------
        fn(
            "ifc_ensure_project_setup",
            "Ensure project+units+Model/Body contexts+Project->Site->Building hierarchy; optionally adds a default storey.",
            {
                "handle": string,
                "project_name": string,
                "site_name": string,
                "building_name": string,
                "ensure_default_storey": boolean,
                "default_storey_name": string,
                "default_storey_elevation_m": number,
            },
            [],
        ),
        fn(
            "ifc_add_storey",
            "Create a storey under a building GUID and place it at elevation.",
            {"handle": string, "building_guid": string, "name": string, "elevation_m": number},
            ["building_guid"],
        ),
        fn(
            "ifc_set_local_placement",
            "Set object placement for an element GUID using a 4x4 matrix.",
            {"handle": string, "element_guid": string, "matrix_4x4": matrix_4x4, "should_transform_children": boolean},
            ["element_guid", "matrix_4x4"],
        ),
        fn(
            "ifc_add_wall",
            "Add a viewable wall by start/end XY with height/thickness, contained in a storey.",
            {
                "handle": string,
                "storey_guid": string,
                "start_xy": vec2,
                "end_xy": vec2,
                "height_m": number,
                "thickness_m": number,
                "base_z_m": number,
                "name": string,
                "centerline": boolean,
            },
            ["storey_guid", "start_xy", "end_xy"],
        ),
        fn(
            "ifc_add_slab",
            "Add a viewable slab from a polyline, contained in a storey.",
            {
                "handle": string,
                "storey_guid": string,
                "polyline_xy": polyline2,
                "depth_m": number,
                "z_m": number,
                "name": string,
                "predefined_type": string,
            },
            ["storey_guid", "polyline_xy"],
        ),
    ]


def run_modifier(
    llm: LLMRunner,
    *,
    user_prompt: str,
    patch_plan: Dict[str, Any],
    ifc_in_path: Optional[str],
    ifc_out_path: str,
    schema: str = "IFC4",
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    tools = modifier_tools_schema()
    state: Dict[str, Any] = {"handle": None, "saved": False}

    if trace_path is None:
        trace_path = os.path.join(os.path.dirname(ifc_out_path) or ".", "modifier_trace.jsonl")

    # Seed with an initial handle to reduce tool churn
    if ifc_in_path:
        seed = tools_ifc.ifc_open(ifc_in_path)
    else:
        seed = tools_ifc.ifc_new(schema=schema)
    state["handle"] = seed.get("handle")

    def handler(name: str, args: Dict[str, Any]) -> Any:
        fn = getattr(tools_ifc, name)
        out = fn(**args)
        if isinstance(out, dict) and "handle" in out:
            state["handle"] = out["handle"]
        if name == "ifc_save" and isinstance(out, dict) and out.get("ok"):
            state["saved"] = True
        return out

    payload = {
        "user_prompt": user_prompt,
        "patch_plan": patch_plan,
        "ifc_in_path": ifc_in_path,
        "output_ifc_path": ifc_out_path,
        "handle": state["handle"],
        "notes": [
            "Use the provided handle as your starting IFC state.",
            "Use ifc_ensure_project_setup early if starting from an empty model.",
            "Prefer ifc_add_wall/ifc_add_slab for viewable structure.",
            "Save to output_ifc_path at the end using ifc_save(handle, path).",
        ],
    }

    text, _ = llm.run_with_tools(
        instructions=MODIFIER_INSTRUCTIONS,
        user_input=payload,
        tools=tools,
        tool_handler=handler,
        trace_path=trace_path,
        trace_tag="modifier",
    )

    # Safety: if LLM forgot to save, save last handle
    if state.get("handle") and not state.get("saved"):
        tools_ifc.ifc_save(handle=state["handle"], path=ifc_out_path)
        state["saved"] = True

    return {"ok": True, "ifc_out_path": ifc_out_path, "modifier_log": text, "modifier_trace_path": trace_path}
