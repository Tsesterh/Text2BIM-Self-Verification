from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from llm import LLMRunner
import tools_ifc


def _safe_json_loads(text: str):
    """Parse JSON from model output; tolerates accidental leading/trailing text."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(1))


REVIEWER_INSTRUCTIONS = """You are a Reviewer agent, responsible for checking if a given specification is met for a given IFC Model (in Building Information Modelling).

You receive ONLY:
- spec_markdown (the contract)
- user_prompt (original user intent; spec_markdown tells you what to check)
- ifc_path (the IFC artifact)

You have read-only tools that can inspect/query the model, including a python query tool.

Task:
- For each requirement in spec_markdown, decide:
    status: pass | fail
- Provide evidence for each decision (tool outputs, computed values).
- Provide fix hints for failures.

Output JSON:
{
  "summary": {"pass": n, "fail": n},
  "checks": [{"requirement_id": "...", "status": "...", "evidence": {...}, "notes": "..."}],
  "issues": [{"requirement_id": "...", "status": "fail", "fix_hint": "...", "evidence": {...}}]
}

Rules:
- Use tools for every factual claim about the IFC.
- Do not invent additional requirements not in the spec.
- Match each requirement_id to the "REQ-###" prefix from spec_markdown.
- In general, focus on the geometric requirements, as they can not be checked by the IDS validator. Make sure the elements are correctly oriented, as specified in the spec_markdown.
- Only give a fail if the requirement is clearly not met; if in doubt, give pass with a note about uncertainty and what to check.
- Do not perform any modification or save operations; use read-only queries only.
Return ONLY valid JSON.
"""


def reviewer_tools_schema() -> List[Dict[str, Any]]:
    def fn(name, desc, params, req):
        return {
            "type": "function",
            "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": params, "required": req}},
        }

    string = {"type": "string"}
    integer = {"type": "integer"}

    return [
        fn("ifc_open", "Open IFC from disk and return a handle.", {"path": string}, ["path"]),
        fn("ifc_inspect", "Get schema and basic entity counts.", {"handle": string}, ["handle"]),
        fn("ifc_select", "Select entities using selector syntax.", {"handle": string, "selector": string, "limit": integer}, ["handle", "selector"]),
        fn("ifc_get", "Get entity info by GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn("ifc_get_psets", "Get all property sets for GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn("ifc_get_materials", "Get materials for GUID.", {"handle": string, "guid": string}, ["handle", "guid"]),
        fn(
            "ifc_python_query",
            "Execute read-only IfcOpenShell python query code (set `result`). No imports; available locals: model/ifc, ifc_path, ifcopenshell, api, util, selector. Basic builtins like str/int/len/hasattr are available.",
            {"handle": string, "ifc_path": string, "code": string},
            ["code"],
        ),
    ]


def run_reviewer(
    llm: LLMRunner,
    spec_md: str,
    ifc_path: str,
    user_prompt: str,
    out_path: str,
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    tools = reviewer_tools_schema()

    if trace_path is None:
        trace_path = os.path.join(os.path.dirname(out_path) or ".", "reviewer_trace.jsonl")

    state: Dict[str, Any] = {"handle": None}
    seed = tools_ifc.ifc_open(ifc_path)
    state["handle"] = seed.get("handle")

    def handler(name: str, args: Dict[str, Any]) -> Any:
        fn = getattr(tools_ifc, name)
        out = fn(**args)
        if isinstance(out, dict) and "handle" in out and name == "ifc_open":
            state["handle"] = out["handle"]
        return out

    response_format = {"type": "json_object"}
    payload = {"spec_markdown": spec_md, "user_prompt": user_prompt, "ifc_path": ifc_path, "handle": state["handle"]}

    text, _ = llm.run_with_tools(
        instructions=REVIEWER_INSTRUCTIONS,
        user_input=payload,
        tools=tools,
        tool_handler=handler,
        response_format=response_format,
        trace_path=trace_path,
        trace_tag="reviewer",
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    return {"ok": True, "review_report_path": out_path, "reviewer_trace_path": trace_path}
