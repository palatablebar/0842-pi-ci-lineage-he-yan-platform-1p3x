import json
from decimal import Decimal
from pathlib import Path

from app.catalog import SPEC
from app.certification.engine import evaluate_certification
from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.intake.service import load
from app.reconcile.ledger import build_ledger


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _stage(name):
    return SPEC["stages"].index(name) + 1


def _certification_report(results, certification):
    """根据请求中的认证口径生成承诺占比，并按需合并判定。"""
    if certification is None:
        return None, False
    config = dict(certification) if isinstance(certification, dict) else {}
    if config.get("enabled") is False:
        return {"enabled": False, "batches": [], "counts": {}}, False
    lots = config.pop("lots", None) or config.pop("batches", None) or results
    apply_decisions = config.pop("apply_decisions", True)
    enabled = config.pop("enabled", True)
    report = evaluate_certification(lots=lots, enabled=enabled, **config)
    by_id = {item["lot_id"]: item for item in report.get("batches", [])}
    severity = {"PASS": 0, "REVIEW": 1, "REJECT": 2}
    for result in results:
        cert = by_id.get(str(result.get(SPEC["key"], "")))
        if not cert:
            continue
        result["certification_status"] = cert["status"]
        result["committed_ratio"] = cert["committed_ratio"]
        result["committed_ratio_text"] = cert["committed_ratio_text"]
        result["certification_basis"] = cert["basis"]
        if apply_decisions and result.get("verdict"):
            current = result.get("verdict", "REVIEW")
            candidate = cert["verdict"]
            if severity.get(candidate, 1) > severity.get(current, 1):
                result["verdict"] = candidate
                result["reason"] = cert["reason"]
    return report, bool(apply_decisions)


def pipeline(profile="dirty", path=None, upto="证据交付", certification=None, rows=None, **cert_kwargs):
    if certification is None and cert_kwargs:
        certification = cert_kwargs
    source_row_data = rows
    if rows is not None:
        import hashlib
        import json as _json
        inline_rows = []
        for index, raw_row in enumerate(rows):
            row = {column: "" for column in SPEC["columns"]}
            if isinstance(raw_row, dict):
                row.update(raw_row)
            row.setdefault("_line", index + 2)
            if row.get("recall", "") == "":
                row["recall"] = 0
            inline_rows.append(row)
        rows = inline_rows
        source_row_data = rows
        digest = hashlib.sha256(_json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        intake = {
            "profile": profile or "inline",
            "path": "inline-payload.json",
            "sha256": digest,
            "rows": rows,
            "issues": [],
            "counts": {"input": len(rows), "accepted": len(rows), "isolated": 0,
                       "duplicates": 0, "ignored": 0},
        }
    else:
        intake = load(profile, path)
    prepared, prepare_issues = [], []
    results = []
    ledger = None
    if _stage(upto) >= 2:
        prepared, prepare_issues = prepare_rows(intake["rows"])
        if rows is not None:
            intake["counts"]["accepted"] = len(prepared)
            intake["counts"]["isolated"] = max(0, intake["counts"]["input"] - len(prepared))
    if _stage(upto) >= 3:
        results = calculate_rows(prepared)
    if _stage(upto) >= 4:
        results = decide_rows(results)
    certification_report = None
    if certification is not None and _stage(upto) >= 3:
        certification_report, _ = _certification_report(results, certification)
    if _stage(upto) >= 5:
        ledger = build_ledger(intake, prepared, prepare_issues, results)
    intake["counts"]["prepared"] = len(prepared)
    intake["counts"]["computed"] = len(results)
    issues = intake["issues"] + prepare_issues
    summary = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for item in results:
        summary[item.get("verdict", "REVIEW")] = summary.get(item.get("verdict", "REVIEW"), 0) + 1
    chart_field = {"0840": "rolling", "0849": "mean", "0850": "settlement_cents"}.get(SPEC["code"], "metric")
    chart = [{"label": item.get("label", item.get(SPEC["key"], "")),
              "value": float(item.get(chart_field) or 0),
              "limit": float(item.get("limit") or 0)}
             for item in results[:12]]
    rows = []
    raw_stage = _stage(upto) <= 2
    if raw_stage:
        stage_rows = intake["rows"] if _stage(upto) == 1 else prepared
        rows = [{key: item.get(key) for key in SPEC["columns"]} for item in stage_rows[:18]]
    else:
        for item in results[:18]:
            rows.append({key: value for key, value in item.items()
                         if key in {"label", "verdict", "reason", "metric", "limit", "status", "state",
                                    "breakdown", "committed_ratio", "committed_ratio_text",
                                    "certification_status", "certification_basis"}
                         or key in SPEC["columns"] or key in SPEC["result_columns"]})
    source = {"profile": profile, "filename": Path(intake["path"]).name, "sha256": intake["sha256"]}
    payload = {
        "project": SPEC["code"] + " " + SPEC["name"],
        "rule": SPEC["rule_short"],
        "profile": profile,
        "source": source,
        "source_rows": source_row_data,
        "counts": intake["counts"],
        "issues": issues[:60],
        "rows": rows,
        "row_kind": "source" if raw_stage else "result",
        "chart": chart,
        "summary": summary,
        "certification": certification_report,
        "ledger": ledger,
        "stages": [{"id": "intake", "name": SPEC["stages"][0]},
                   {"id": "prepare", "name": SPEC["stages"][1]},
                   {"id": "calculate", "name": SPEC["stages"][2]},
                   {"id": "decision", "name": SPEC["stages"][3]},
                   {"id": "reconcile", "name": SPEC["stages"][4]}],
        "run_key": __import__("hashlib").sha256(
            (SPEC["code"] + profile + intake["sha256"] + SPEC["rule"]).encode("utf-8")).hexdigest()[:24],
    }
    if certification_report:
        payload["commitments"] = certification_report.get("commitments", [])
        payload["certified_ratios"] = certification_report.get("commitment_map", {})
        payload["certification_summary"] = certification_report.get("counts", {})
        payload["sorting_records"] = certification_report.get("records", [])
        for key in ("guaranteed_lots", "review_lots", "pending_lots", "rejected_lots", "signable_lots"):
            payload[key] = certification_report.get(key, [])
    return jsonable(payload)
