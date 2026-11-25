from flask import Blueprint, current_app, jsonify, request

from app.certification.engine import evaluate_certification
from app.certification.service import build_report, register_records
from app.errors import DomainIssue
from app.review.service import serialize
from app.service import pipeline


certification_api = Blueprint("certification_api", __name__)


def cert_store():
    return current_app.extensions["certification_store"]


def run_store():
    return current_app.extensions["runs"]


def json_body():
    data = request.get_json(silent=True) or {}
    idem = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    if idem and isinstance(data, dict):
        data.setdefault("record_id", idem)
    return data


@certification_api.post("/api/certification/evaluate")
@certification_api.post("/api/certification/calculate")
@certification_api.post("/api/certification/commitments")
@certification_api.post("/api/certified-ratios")
@certification_api.post("/api/commitments")
@certification_api.post("/api/certification/workbench")
@certification_api.post("/api/certification/run")
@certification_api.post("/api/certification/commit")
@certification_api.post("/api/certification")
@certification_api.post("/api/certify")
def evaluate():
    data = json_body()
    return jsonify(build_report(data))


@certification_api.get("/api/certification/workbench")
def workbench():
    scope = request.args.get("scope", "global")
    stored = cert_store().list_records(scope)
    state = cert_store().get_state(scope) or {}
    threshold = request.args.get("threshold", request.args.get("required_ratio", state.get("threshold")))
    if state.get("lots"):
        config = dict(state)
        config["threshold"] = threshold if threshold is not None else config.get("threshold")
        report = evaluate_certification(records=stored, **config)
    else:
        profile = request.args.get("profile", "clean")
        config = {"enabled": True, "apply_decisions": False}
        if threshold is not None:
            config["threshold"] = threshold
        payload = pipeline(profile, certification=config)
        report = payload["certification"]
        report["records"] = stored
    return jsonify(report)


@certification_api.post("/api/certification/sorting-records")
@certification_api.post("/api/certification/records")
@certification_api.post("/api/sorting-records")
@certification_api.post("/api/sorting_records")
@certification_api.post("/api/sorting")
@certification_api.post("/api/allocations")
@certification_api.post("/api/loss-records")
@certification_api.post("/api/records")
def sorting_records():
    raw_data = json_body()
    data = raw_data if isinstance(raw_data, dict) else {"sorting_records": raw_data}
    scope = str(data.get("scope") or "global")
    if any(key in data for key in ("lots", "batches", "rows", "threshold", "required_ratio", "certified", "sources")):
        cert_store().save_state(scope, {
            "lots": data.get("lots") or data.get("batches") or data.get("rows") or [],
            "threshold": data.get("threshold", data.get("required_ratio")),
            "certified": data.get("certified") or data.get("sources"),
        })
    return jsonify(register_records(cert_store(), data, scope))


@certification_api.post("/api/certification/recalculate")
def recalculate():
    data = json_body()
    scope = str(data.get("scope") or "global")
    stored = cert_store().list_records(scope)
    state = cert_store().get_state(scope) or {}
    config = {**state, **data}
    config.pop("scope", None)
    return jsonify(evaluate_certification(records=stored, **config))


def _run_cert_config(data, stored_records, original=None):
    original = original or {}
    config = {
        "threshold": data.get("threshold", data.get("required_ratio", original.get("threshold"))),
        "certified": data.get("certified") or data.get("sources") or original.get("certified") or original.get("sources"),
        "apply_decisions": data.get("apply_decisions", original.get("apply_decisions", True)),
        "records": stored_records,
    }
    lots = data.get("lots") or data.get("batches") or original.get("lots") or original.get("batches")
    if lots is not None:
        config["lots"] = lots
    return {key: value for key, value in config.items() if value is not None}


@certification_api.post("/api/runs/<run_id>/sorting-records")
@certification_api.post("/api/runs/<run_id>/sorting-record")
@certification_api.post("/api/runs/<run_id>/sorting_records")
@certification_api.post("/api/runs/<run_id>/sorting")
@certification_api.post("/api/runs/<run_id>/allocations")
@certification_api.post("/api/runs/<run_id>/loss-records")
@certification_api.post("/api/runs/<run_id>/certification/records")
@certification_api.post("/api/runs/<run_id>/certification-records")
@certification_api.post("/api/runs/<run_id>/records")
def run_sorting_records(run_id):
    row = run_store().get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    raw_data = json_body()
    data = raw_data if isinstance(raw_data, dict) else {"sorting_records": raw_data}
    scope = f"run:{run_id}"
    result = register_records(cert_store(), data, scope)
    stored_records = cert_store().list_records(scope)

    run = serialize(row)
    profile = run.get("profile", "clean")
    config = _run_cert_config(data, stored_records, run.get("certification_config"))
    source_rows = run.get("source_rows")
    if "lots" not in config and source_rows:
        config["lots"] = source_rows
    if "lots" not in config:
        fresh = pipeline(profile, certification=config)
    else:
        lots = config.pop("lots")
        fresh = pipeline(profile if profile != "inline" else "inline",
                         certification=config,
                         rows=lots if source_rows is not None or profile == "inline" else None)
        # 样例运行也允许请求体临时带 lots；此时直接无状态报告，避免误读样例。
        if profile != "inline" and source_rows is None:
            report = evaluate_certification(lots=lots, **config)
            fresh = dict(run)
            fresh["certification"] = report
    # 保留运行身份与复核状态；晚到证据只更新载荷，不伪造复核版本。
    if run.get("certification_config"):
        fresh["certification_config"] = run["certification_config"]
    if source_rows is not None:
        fresh["source_rows"] = source_rows
    fresh["run_id"] = row["id"]
    fresh["status"] = row["status"]
    fresh["revision"] = row["revision"]
    fresh["comment"] = row["comment"]
    fresh["created_at"] = row["created_at"]
    updated = run_store().update_payload(run_id, fresh)
    response = serialize(updated)
    response["posting"] = result["posting"]
    record_lots = {item.get("lot_id") for item in data if isinstance(item, dict)} if isinstance(data, list) else {data.get("lot_id")}
    record_lots.discard(None)
    affected = set(result.get("affected_lot_ids", [])) | set(record_lots)
    cert = response.get("certification") or {}
    for batch in cert.get("batches", []):
        if record_lots & set(batch.get("ancestor_lot_ids", [])):
            affected.add(batch.get("lot_id"))
    response["affected_lot_ids"] = sorted(x for x in affected if x)
    response["posting"]["affected_lot_ids"] = response["affected_lot_ids"]
    response["added"] = result.get("added", 0)
    response["duplicate"] = result.get("duplicate", False)
    response["duplicate_count"] = result.get("duplicate_count", 0)
    response["record"] = result.get("posting", {}).get("outcomes", [{}])[0]
    run_view = dict(response)
    response["run"] = run_view
    return jsonify(response)


@certification_api.get("/api/runs/<run_id>/certification")
def run_certification(run_id):
    row = run_store().get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    run = serialize(row)
    if run.get("certification"):
        return jsonify(run["certification"])
    records = cert_store().list_records(f"run:{run_id}")
    return jsonify(evaluate_certification(run.get("rows", []), records=records))
