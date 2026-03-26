from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


TOOL_GUIDANCE = (
    "Use `execute_ifc_code` (or `execute_ifc_code_tool`) for IFC inspection and calculations. "
    "Use other tools only if they are strictly read-only. "
    "Do NOT call any tool that creates, modifies, deletes, saves, or exports IFC data. "
    "Avoid repeated tool calls with the same query."
)

REVIEWER_SYSTEM_PROMPT = (
    "You are a Reviewer agent. You only inspect and report. "
    "You must not modify the IFC in any way."
)


def _safe_json_loads(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(1))


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("output", "content", "final", "text"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]
            if hasattr(last, "content") and isinstance(last.content, str):
                return last.content
    if hasattr(result, "content") and isinstance(result.content, str):
        return result.content
    return str(result)


@dataclass
class MCPReviewerConfig:
    model_name: str
    system_prompt: str
    mcp_servers: Dict[str, Any]


def _load_mcp_config(config_path: str) -> MCPReviewerConfig:
    cfg = _load_json(config_path)
    return MCPReviewerConfig(
        model_name=cfg["model_name"],
        system_prompt=cfg.get("system_prompt", ""),
        mcp_servers=cfg["mcp_servers"],
    )


async def _run_mcp_reviewer_async(
    *,
    config_path: str,
    spec_md: str,
    ifc_path: str,
    user_prompt: str,
    out_path: str,
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain.chat_models import init_chat_model
        from langchain.agents import create_agent
    except Exception as exc:
        raise RuntimeError(
            "Missing MCP/LangChain dependencies. Install langchain, langgraph, and langchain-mcp-adapters."
        ) from exc

    if not os.path.exists(ifc_path):
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")

    cfg = _load_mcp_config(config_path)

    client = MultiServerMCPClient(cfg.mcp_servers)
    mcp_tools = await client.get_tools()

    # Prefer load_ifc_file/load_ifc_filepath if present
    load_ifc_tool = next(
        (
            t
            for t in mcp_tools
            if t.name in ("load_ifc_file", "load_ifc_filepath") or t.name.endswith("load_ifc_file")
        ),
        None,
    )
    if load_ifc_tool is not None:
        await load_ifc_tool.ainvoke(
            {
                "filepath": str(Path(ifc_path).resolve()),
                "use_relative_path": False,
                "start_fresh_session": True,
            }
        )

    model = init_chat_model(cfg.model_name)
    agent = create_agent(model, mcp_tools)

    system_prompt = (cfg.system_prompt + "\n\n" + REVIEWER_SYSTEM_PROMPT).strip()
    user_content = (
        "You are reviewing an IFC model using MCP tools.\n\n"
        f"User prompt:\n{user_prompt}\n\n"
        f"Specification (spec_markdown):\n{spec_md}\n\n"
        f"IFC path (already loaded): {str(Path(ifc_path).resolve())}\n\n"
        "Return ONLY valid JSON with this schema:\n"
        "{\n"
        '  "summary": {"pass": n, "fail": n},\n'
        '  "checks": [{"requirement_id": "...", "status": "pass|fail", "evidence": {...}, "notes": "..."}],\n'
        '  "issues": [{"requirement_id": "...", "status": "fail", "fix_hint": "...", "evidence": {...}}]\n'
        "}\n\n"
        "Rules:\n"
        "- Match requirement_id to REQ-### prefixes in spec_markdown.\n"
        "- Use tools for every factual claim about the IFC.\n"
        "- Do NOT modify the IFC or save/export anything.\n\n"
        f"{TOOL_GUIDANCE}"
    )

    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ]
            }
        )
    except Exception as exc:
        raise RuntimeError(f"MCP reviewer failed: {exc}") from exc

    text = _extract_text(result)
    payload = _safe_json_loads(text)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if trace_path:
        os.makedirs(os.path.dirname(trace_path) or ".", exist_ok=True)
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write(text)

    return {"ok": True, "review_report_path": out_path, "reviewer_trace_path": trace_path}


def run_mcp_reviewer(
    *,
    config_path: str,
    spec_md: str,
    ifc_path: str,
    user_prompt: str,
    out_path: str,
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    return asyncio.run(
        _run_mcp_reviewer_async(
            config_path=config_path,
            spec_md=spec_md,
            ifc_path=ifc_path,
            user_prompt=user_prompt,
            out_path=out_path,
            trace_path=trace_path,
        )
    )
