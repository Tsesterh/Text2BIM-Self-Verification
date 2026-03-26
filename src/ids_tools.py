from __future__ import annotations

import json
from typing import Any, Dict

import ifcopenshell
from ifctester import ids, reporter


def run_ids_check(ids_path: str, ifc_path: str, report_path: str) -> Dict[str, Any]:
    """
    Validate IFC against an IDS (IfcTester).

    Produces:
      - JSON report at report_path
      - normalized summary for downstream merge

    Notes:
      - validate=True enforces IDS XSD compliance early.
      - reporter.Json has had version-specific crashes; we provide a fallback.
    """
    specs = ids.open(ids_path, validate=True)
    model = ifcopenshell.open(ifc_path)

    specs.validate(model)

    raw: Dict[str, Any]
    try:
        # Json reporter is broken in ifctester 0.8.4 (cardinality bug).
        # Use Txt reporter for a human-readable report, and fall back to Ids.asdict for normalization.
        rep = reporter.Txt(specs)
        rep.report()
        rep.to_file(report_path)
        raw = {"warning": "txt_report_used", "ids": specs.asdict()}
    except Exception as e:
        # If we can't produce a report, stop the pipeline.
        raise RuntimeError(f"IDS txt reporter failed: {e}") from e

    normalized = normalize_ifctester_report(raw)
    return {"ok": True, "ids_report_path": report_path, "normalized": normalized}


def normalize_ifctester_report(report_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort normalization across versions and across our fallback path.
    """
    # If reporter.Json worked, it usually has "specifications"/"specs".
    # If fallback path, we have {"ids": Ids.asdict()}.
    if "ids" in report_json and isinstance(report_json["ids"], dict):
        ids_dict = report_json["ids"]
        specs = ids_dict.get("specifications", {}) or {}
        return _normalize_from_ids_asdict(specs)

    specs = report_json.get("specifications") or report_json.get("specs") or []
    results = []
    for s in specs:
        req_id = s.get("identifier") or s.get("id") or s.get("name")
        status_raw = s.get("status")
        if isinstance(status_raw, bool):
            status = "pass" if status_raw else "fail"
        else:
            status = (str(status_raw or "")).lower() or "unknown"

        violations = []
        for v in s.get("failed_entities", []) or s.get("failures", []) or []:
            if isinstance(v, dict):
                guid = v.get("GlobalId") or v.get("guid")
                reason = v.get("reason") or v.get("message") or "failed"
            else:
                guid = getattr(v, "GlobalId", None)
                reason = "failed"
            violations.append({"guid": guid, "reason": reason})

        results.append({"requirement_id": req_id, "status": status, "violations": violations})

    summary = {
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "unknown": sum(1 for r in results if r["status"] not in ("pass", "fail")),
    }
    return {"summary": summary, "results": results}


def _normalize_from_ids_asdict(specs: Any) -> Dict[str, Any]:
    """
    Handle Ids.asdict() shape.
    Typical structure:
      {"specification": [ ... ]} or directly [ ... ]
    """
    specs_list: List[Dict[str, Any]] = []
    if isinstance(specs, dict):
        maybe = specs.get("specification")
        if isinstance(maybe, list):
            specs_list = maybe
        elif isinstance(maybe, dict):
            specs_list = [maybe]
    elif isinstance(specs, list):
        specs_list = specs

    results = []
    for s in specs_list or []:
        if not isinstance(s, dict):
            continue
        req_id = s.get("identifier") or s.get("name") or s.get("@name")
        status_raw = s.get("status")
        if isinstance(status_raw, bool):
            status = "pass" if status_raw else "fail"
        else:
            status = (str(status_raw or "")).lower() or "unknown"

        # asdict() doesn’t necessarily include failed entities details,
        # but we can keep it consistent.
        results.append({"requirement_id": req_id, "status": status, "violations": []})

    summary = {
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "unknown": sum(1 for r in results if r["status"] not in ("pass", "fail")),
    }
    return {"summary": summary, "results": results}
