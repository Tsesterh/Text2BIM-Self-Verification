from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from llm import LLMRunner
from ids_builder import write_ids_from_plan, ids_facet_list, ids_facet_schema, ids_plan_validate


def _safe_json_loads(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(1))


SPECIFIER_INSTRUCTIONS = """You are the Specifier agent for a BIM/IFC generation system.

You must output JSON ONLY.

You DO NOT write IDS XML.
Instead, you write an IDS_PLAN that will be compiled into valid IDS XML by a Python tool.

Output JSON schema:
{
  "spec_markdown": "markdown text",
  "ids_plan": {
     "title": "...",
     "description": "...",
     "version": "1.0",
     "date": "YYYY-MM-DD",
     "specifications": [
        {
          "name": "...",
          "ifcVersion": ["IFC4"],
          "identifier": "optional",
          "description": "optional",
          "instructions": "optional",
          "minOccurs": 0,
          "maxOccurs": "unbounded",
          "applicability": [
            {"facet_type": "Entity", "args": {"name": "IfcSpace", "predefinedType": "INTERNAL"}}
          ],
          "requirements": [
            {"facet_type": "Attribute", "args": {"name": "CompositionType", "value": "ELEMENT"}}
          ]
        }
     ]
  },
  "assumptions": ["optional list of assumptions"]
}

Rules:
- Keep spec_markdown short and pragmatic (no “severity” taxonomy).
- spec_markdown MUST be a list of atomic requirements, one per line, each starting with "REQ-###:" (e.g., REQ-001: ...).
- Each requirement must be verifiable from the IFC using inspection or basic geometry calculations.
- spec_markdown should contain requirements that are NOT expressible in IDS (e.g., complex relations, geometry constraints). It should especially check the geometric validity of elements (e.g. correct orientation, no self-intersecting polygons, etc.) since this is not easily done in IDS.
- ids_plan and spec_markdown MUST be disjoint (no duplicate requirements).
- Use concrete quantities and units from the user_prompt. If something is missing, add a clear assumption in "assumptions".
- Use typical IDS concepts: entity/applicability + attribute/property/material/classification constraints.
- Before selecting facet_type and args, discover what facets exist and their parameters using the provided tools.
- Only include facets you are confident the tool can compile.
- If something is not machine-checkable in IDS, keep it in spec_markdown only and do not put it into ids_plan.

Return ONLY valid JSON.
"""


def specifier_tools_schema():
    def fn(name, desc, params, req):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": params, "required": req},
            },
        }

    return [
        fn("ids_facet_list", "List available IfcTester IDS facet classes.", {}, []),
        fn(
            "ids_facet_schema",
            "Get constructor signature / schema for a facet class.",
            {"facet_type": {"type": "string"}},
            ["facet_type"],
        ),
        fn(
            "ids_plan_validate",
            "Validate an ids_plan by compiling and schema-validating it (no files written).",
            {"plan": {"type": "object"}},
            ["plan"],
        ),
    ]


def run_specifier(llm: LLMRunner, user_prompt: str, out_dir: str, trace_path: Optional[str] = None) -> Dict[str, Any]:
    tools = specifier_tools_schema()

    if trace_path is None:
        trace_path = os.path.join(out_dir, "specifier_trace.jsonl")

    def handler(name: str, args: Dict[str, Any]) -> Any:
        if name == "ids_facet_list":
            return ids_facet_list()
        if name == "ids_facet_schema":
            return ids_facet_schema(**args)
        if name == "ids_plan_validate":
            return ids_plan_validate(**args)
        raise ValueError(f"Unknown tool: {name}")

    response_format = {"type": "json_object"}

    text, _ = llm.run_with_tools(
        instructions=SPECIFIER_INSTRUCTIONS,
        user_input=user_prompt,
        tools=tools,
        tool_handler=handler,
        response_format=response_format,
        trace_path=trace_path,
        trace_tag="specifier",
    )

    payload = _safe_json_loads(text)

    os.makedirs(out_dir, exist_ok=True)

    spec_path = f"{out_dir}/spec.md"
    ids_path = f"{out_dir}/requirements.ids"
    assumptions_path = f"{out_dir}/assumptions.json"
    ids_meta_path = f"{out_dir}/ids_build_meta.json"

    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(payload["spec_markdown"])

    # Compile IDS using Python (guaranteed schema correctness or hard-fail here)
    ids_result = write_ids_from_plan(payload["ids_plan"], ids_path)

    with open(ids_meta_path, "w", encoding="utf-8") as f:
        json.dump({"warnings": ids_result.get("warnings", [])}, f, indent=2)

    if "assumptions" in payload:
        with open(assumptions_path, "w", encoding="utf-8") as f:
            json.dump(payload["assumptions"], f, indent=2)
    else:
        assumptions_path = None

    return {
        "ok": True,
        "spec_path": spec_path,
        "ids_path": ids_path,
        "assumptions_path": assumptions_path,
        "ids_meta_path": ids_meta_path,
        "specifier_trace_path": trace_path,
    }
