from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from llm import LLMRunner
from specifier import run_specifier
from modifier import run_modifier
from mcp_modifier import run_mcp_modifier
from reviewer import run_reviewer
from mcp_reviewer import run_mcp_reviewer
from ids_tools import run_ids_check
from merge import merge_reports


def orchestrate(
    *,
    user_prompt: str,
    out_dir: str = "run_out",
    model_specifier: str = "gpt-5",
    model_modifier: str = "gpt-5",
    model_reviewer: str = "gpt-5",
    modifier_backend: str = "llm",
    reviewer_backend: str = "llm",
    mcp_config_path: Optional[str] = None,
    max_iterations: int = 6,
) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)

    print("Retrieving initial specification...")
    # 1) Specifier
    spec_llm = LLMRunner(model=model_specifier)
    spec_trace = os.path.join(out_dir, "specifier_trace.jsonl")
    spec_out = run_specifier(spec_llm, user_prompt, out_dir, trace_path=spec_trace)

    print("Specification retrieved.")
    spec_path = spec_out["spec_path"]
    ids_path = spec_out["ids_path"]
    spec_md = open(spec_path, "r", encoding="utf-8").read()

    ifc_path: Optional[str] = None
    final: Dict[str, Any] = {}

    for it in range(1, max_iterations + 1):
        print("Starting iteration ", it)
        print("Starting Modification Loop...")
        iter_dir = os.path.join(out_dir, f"iter_{it}")
        os.makedirs(iter_dir, exist_ok=True)

        # 2) Modifier
        patch_plan = final.get("patch_plan") or {"patch_plan": [], "done_when": []}
        ifc_out = os.path.join(iter_dir, "model.ifc")
        mod_trace = os.path.join(iter_dir, "modifier_trace.jsonl")
        if modifier_backend == "mcp":
            if not mcp_config_path:
                raise ValueError("modifier_backend='mcp' requires mcp_config_path")
            mod_result = run_mcp_modifier(
                config_path=mcp_config_path,
                user_prompt=user_prompt,
                patch_plan=patch_plan,
                ifc_in_path=ifc_path,
                ifc_out_path=ifc_out,
                trace_path=mod_trace,
            )
        else:
            mod_llm = LLMRunner(model=model_modifier)
            mod_result = run_modifier(
                mod_llm,
                user_prompt=user_prompt,
                patch_plan=patch_plan,
                ifc_in_path=ifc_path,
                ifc_out_path=ifc_out,
                trace_path=mod_trace,
            )

        print("Modification iteration ", it, " completed.")
        print("Now Reviewing...")
        ifc_path = mod_result["ifc_out_path"]

        # 3) Reviewer (spec.md + IFC only)
        review_report_path = os.path.join(iter_dir, "review_report.json")
        reviewer_trace = os.path.join(iter_dir, "reviewer_trace.jsonl")
        if reviewer_backend == "mcp":
            if not mcp_config_path:
                raise ValueError("reviewer_backend='mcp' requires mcp_config_path")
            run_mcp_reviewer(
                config_path=mcp_config_path,
                spec_md=spec_md,
                ifc_path=ifc_path,
                user_prompt=user_prompt,
                out_path=review_report_path,
                trace_path=reviewer_trace,
            )
        else:
            review_llm = LLMRunner(model=model_reviewer)
            run_reviewer(review_llm, spec_md, ifc_path, user_prompt, review_report_path, trace_path=reviewer_trace)
        with open(review_report_path, "r", encoding="utf-8") as f:
            review_report = json.load(f)

        print("Review iteration ", it, " completed.")
        print("Now running IDS check...")

        # 4) IDS check (separate)
        ids_report_path = os.path.join(iter_dir, "ids_report.json")
        ids_out = run_ids_check(ids_path, ifc_path, ids_report_path)
        ids_norm = ids_out["normalized"]

        print("IDS check iteration ", it, " completed.")

        # 5) Merge
        merged = merge_reports(review_report, ids_norm)

        final = {
            "iteration": it,
            "ifc_path": ifc_path,
            "spec_path": spec_path,
            "ids_path": ids_path,
            "review_report_path": review_report_path,
            "ids_report_path": ids_report_path,
            "reviewer_trace_path": reviewer_trace,
            "modifier_trace_path": mod_trace,
            "specifier_trace_path": spec_trace,
            "review_summary": review_report.get("summary"),
            "ids_summary": ids_norm.get("summary"),
            "patch_plan": merged,
            "modifier_log": mod_result.get("modifier_log"),
        }

        with open(os.path.join(out_dir, "final_summary.json"), "w", encoding="utf-8") as f:
            json.dump(final, f, indent=2)

        # Stop rule: no P0 + no fails in reviewer + no fails in IDS
        p0 = [p for p in merged.get("patch_plan", []) if p.get("priority") == "P0"]
        reviewer_fail = (review_report.get("summary") or {}).get("fail", 0) or 0
        ids_fail = (ids_norm.get("summary") or {}).get("fail", 0) or 0
        if not p0 and reviewer_fail == 0 and ids_fail == 0:
            break

    return final
