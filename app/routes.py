from flask import Blueprint, abort, jsonify, request, send_file
import hashlib
import json

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def certification_config(data):
    """Accept either {"certification": {...}} or certification fields at top level."""
    config = data.get("certification")
    if isinstance(config, dict):
        config = dict(config)
    else:
        keys = ("threshold", "required_ratio", "minimum_ratio", "min_ratio", "agreed_ratio",
                "agreement_ratio", "certified_threshold", "certification_threshold",
                "required_certified_ratio", "min_certified_ratio", "agreement",
                "certified", "sources",
                "sorting_records", "records", "losses", "loss_events",
                "certified_material_qty", "initial_certified_qty",
                "lots", "batches", "apply_decisions")
        if not any(key in data for key in keys):
            return None
        config = {key: data[key] for key in keys if key in data}
    if "sorting_records" in config and "records" not in config:
        config["records"] = config.pop("sorting_records")
    if "agreed_ratio" in config and "threshold" not in config:
        config["threshold"] = config.pop("agreed_ratio")
    config.setdefault("enabled", True)
    return config


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    inline_rows = data.get("lots") or data.get("batches")
    profile = data.get("profile") or ("inline" if inline_rows is not None else "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    user_config = certification_config(data)
    pipeline_config = user_config or {"enabled": True, "apply_decisions": False}
    return jsonify(pipeline(profile, upto=upto, certification=pipeline_config, rows=inline_rows))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    inline_rows = data.get("lots") or data.get("batches")
    profile = data.get("profile") or ("inline" if inline_rows is not None else "dirty")
    user_config = certification_config(data)
    pipeline_config = user_config or {"enabled": True, "apply_decisions": False}
    payload = pipeline(profile, certification=pipeline_config, rows=inline_rows)
    if user_config is not None:
        payload["certification_config"] = user_config
        digest = hashlib.sha256(json.dumps(user_config, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]
        payload["run_key"] = f"{payload['run_key']}:cert:{digest}"
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
