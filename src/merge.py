from __future__ import annotations

from typing import Any, Dict, List


def merge_reports(review_report: Dict[str, Any], ids_normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Merge Reviewer + IDS results into a patch plan.

    Output:
    {
      "patch_plan": [
        {"priority":"P0|P1|P2", "requirement_id":"...", "source":"Reviewer|IDS", "task":"...", "evidence":...}
      ],
      "done_when": [...]
    }
    """
    patch: List[Dict[str, Any]] = []

    for issue in review_report.get("issues", []) or []:
        rid = issue.get("requirement_id") or issue.get("id")
        status = issue.get("status", "fail")
        if status == "pass":
            continue
        patch.append(
            {
                "priority": "P0" if status == "fail" else "P1",
                "requirement_id": rid,
                "source": "Reviewer",
                "task": issue.get("fix_hint") or issue.get("note") or "Fix requirement",
                "evidence": issue.get("evidence"),
            }
        )

    for r in ids_normalized.get("results", []) or []:
        if r.get("status") != "fail":
            continue
        patch.append(
            {
                "priority": "P0",
                "requirement_id": r.get("requirement_id"),
                "source": "IDS",
                "task": "Fix IDS violation",
                "evidence": r.get("violations"),
            }
        )

    # Deduplicate by (requirement_id, source)
    seen = set()
    dedup = []
    for p in patch:
        key = (p.get("requirement_id"), p.get("source"))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(p)

    pr_order = {"P0": 0, "P1": 1, "P2": 2}
    dedup.sort(key=lambda x: pr_order.get(x.get("priority", "P2"), 9))

    return {"patch_plan": dedup, "done_when": ["no_P0_remaining"]}
