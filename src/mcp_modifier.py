from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import tools_ifc

TOOL_GUIDANCE = (
    "Use the tool `execute_ifc_code_tool` for IFC inspection or calculations when possible. "
    "Use other IFC/MCP tools only if they are the best fit for the task. "
    "Avoid repeated tool calls with the same query. "
    "If the patch plan is empty, build a minimal model that satisfies the user prompt. "
    "If the patch plan is non-empty, focus only on those items and avoid unrelated edits. "
    "When you have enough information, stop and return the final answer."
)


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class MCPModifierConfig:
    model_name: str
    system_prompt: str
    mcp_servers: Dict[str, Any]


def _load_mcp_config(config_path: str) -> MCPModifierConfig:
    cfg = _load_json(config_path)
    return MCPModifierConfig(
        model_name=cfg["model_name"],
        system_prompt=cfg.get("system_prompt", ""),
        mcp_servers=cfg["mcp_servers"],
    )


async def _run_mcp_modifier_async(
    *,
    config_path: str,
    user_prompt: str,
    patch_plan: Dict[str, Any],
    ifc_in_path: Optional[str],
    ifc_out_path: str,
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

    cfg = _load_mcp_config(config_path)

    out_dir = os.path.dirname(ifc_out_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    # Ensure an on-disk IFC exists so Blender can load it.
    if ifc_in_path and os.path.exists(ifc_in_path):
        # If we have an input IFC, copy to output so MCP edits mutate the output file.
        shutil.copyfile(ifc_in_path, ifc_out_path)
    elif not os.path.exists(ifc_out_path):
        # Seed a minimal IFC so Blender can load it; let Blender build the model.
        seed = tools_ifc.ifc_new(schema="IFC4")
        handle = seed.get("handle")
        if handle:
            # Create a bare project entity to avoid empty-file edge cases.
            try:
                tools_ifc.ifc_api(handle=handle, action="root.create_entity", kwargs={"ifc_class": "IfcProject", "name": "Project"})
            except Exception:
                pass
            tools_ifc.ifc_save(handle=handle, path=ifc_out_path)

    client = MultiServerMCPClient(cfg.mcp_servers)
    mcp_tools = await client.get_tools()

    # Prefer load_ifc_file if present
    load_ifc_tool = next(
        (t for t in mcp_tools if t.name == "load_ifc_file" or t.name.endswith("load_ifc_file")),
        None,
    )
    if load_ifc_tool is not None:
        await load_ifc_tool.ainvoke(
            {
                "filepath": str(Path(ifc_out_path).resolve()),
                "use_relative_path": False,
                "start_fresh_session": True,
            }
        )

    model = init_chat_model(cfg.model_name)
    agent = create_agent(model, mcp_tools)

    user_content = (
        "You are modifying an IFC model using MCP tools.\n"
        f"User prompt:\n{user_prompt}\n\n"
        f"Patch plan:\n{json.dumps(patch_plan, indent=2)}\n\n"
        f"Output IFC path (already loaded): {str(Path(ifc_out_path).resolve())}\n\n"
        "IMPORTANT: You must save/export the IFC after edits. Use execute_ifc_code_tool "
        "or execute_blender_code to write the IFC to the output path above.\n\n"
        f"{TOOL_GUIDANCE}"
    )

    # Run agent once; tool calls happen within the agent
    try:
        _ = await agent.ainvoke(
            {
                "messages": [
                    {"role": "system", "content": cfg.system_prompt},
                    {"role": "user", "content": user_content},
                ]
            }
        )
    except Exception as exc:
        raise RuntimeError(f"MCP modifier failed: {exc}") from exc

    # MCP tools mutate the loaded IFC; assume changes persisted to ifc_out_path.
    return {
        "ok": True,
        "ifc_out_path": ifc_out_path,
        "modifier_log": "MCP modifier completed. See MCP server logs for tool-level details.",
        "modifier_trace_path": trace_path,
    }


def run_mcp_modifier(
    *,
    config_path: str,
    user_prompt: str,
    patch_plan: Dict[str, Any],
    ifc_in_path: Optional[str],
    ifc_out_path: str,
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    return asyncio.run(
        _run_mcp_modifier_async(
            config_path=config_path,
            user_prompt=user_prompt,
            patch_plan=patch_plan,
            ifc_in_path=ifc_in_path,
            ifc_out_path=ifc_out_path,
            trace_path=trace_path,
        )
    )
