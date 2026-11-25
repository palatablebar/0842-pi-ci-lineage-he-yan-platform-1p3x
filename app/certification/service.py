"""认证占比工作台的应用服务层。"""

from __future__ import annotations

from typing import Any

from app.certification.engine import (
    CertificationError,
    evaluate_certification,
    extract_record_payload,
    normalize_record,
)
from app.errors import DomainIssue


def _raise_domain(exc: CertificationError):
    code = str(exc) or "CERTIFICATION_BAD_REQUEST"
    status = 409 if code == "RECORD_CONFLICT" else 400
    raise DomainIssue(code, status)


def build_report(data: Any = None) -> dict[str, Any]:
    """从 HTTP 请求体/配置字典生成报告，不写幂等台账。"""
    data = data if isinstance(data, dict) else {"lots": data}
    try:
        return evaluate_certification(data)
    except CertificationError as exc:
        _raise_domain(exc)


def register_records(store, data: dict[str, Any], scope: str = "global") -> dict[str, Any]:
    """持久化一份或多份分拣记录，并返回幂等结果与最新核算。"""
    records_payload = extract_record_payload(data)
    if not records_payload:
        # 也允许直接 POST 单条记录。
        records_payload = [data]

    outcomes = []
    try:
        for raw in records_payload:
            record = normalize_record(raw)
            status, row = store.add_record(
                scope,
                record["record_id"],
                record["digest"],
                record["lot_id"],
                {
                    "record_id": record["record_id"],
                    "lot_id": record["lot_id"],
                    "loss_id": record["loss_id"],
                    "certified_loss_qty": str(record["certified_loss_qty"]),
                    "uncertified_loss_qty": str(record["uncertified_loss_qty"]),
                    "digest": record["digest"],
                },
            )
            if status == "conflict":
                raise DomainIssue("RECORD_CONFLICT", 409)
            outcomes.append({
                "record_id": record["record_id"],
                "status": status,
                "duplicate": status == "duplicate",
                "created": status == "created",
            })
    except CertificationError as exc:
        _raise_domain(exc)

    stored_records = store.list_records(scope)
    config = store.get_state(scope) or {}
    lots = data.get("lots") or data.get("batches") or data.get("rows") or config.get("lots", [])
    threshold = data.get("threshold", data.get("required_ratio", config.get("threshold")))
    certified = data.get("certified") or data.get("sources") or config.get("certified")
    try:
        report = evaluate_certification(
            lots=lots,
            records=stored_records,
            threshold=threshold,
            certified=certified,
        )
    except CertificationError as exc:
        _raise_domain(exc)

    record_lots = {
        normalize_record(raw).get("lot_id")
        for raw in records_payload if isinstance(raw, dict)
    }
    record_lots.discard("")
    created_count = sum(1 for item in outcomes if item["created"])
    duplicate_count = sum(1 for item in outcomes if item["duplicate"])
    affected = set(record_lots)
    for batch in report.get("batches", []):
        ancestors = set(batch.get("ancestor_lot_ids", []))
        if record_lots & ancestors:
            affected.add(batch["lot_id"])
    posting = {
        "scope": scope,
        "outcomes": outcomes,
        "record_ids": [item["record_id"] for item in outcomes],
        "created": created_count,
        "added": created_count,
        "duplicate_count": duplicate_count,
        "duplicate": created_count == 0 and bool(outcomes),
        "affected_lot_ids": sorted(affected),
    }
    report["posting"] = posting
    report["added"] = created_count
    report["created_count"] = created_count
    report["duplicate_count"] = duplicate_count
    report["duplicate"] = posting["duplicate"]
    report["affected_lot_ids"] = sorted(affected)
    return report


class CertificationLedger:
    """无数据库时也可直接使用的内存幂等台账。"""

    def __init__(self, lots: Any = None, threshold: Any = None):
        self.lots = lots or []
        self.threshold = threshold
        self.records: dict[str, dict[str, Any]] = {}

    def post(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_record(record)
        existing = self.records.get(normalized["record_id"])
        if existing is not None and existing["digest"] != normalized["digest"]:
            raise CertificationError("RECORD_CONFLICT")
        duplicate = existing is not None
        if not duplicate:
            self.records[normalized["record_id"]] = normalized
        report = self.workbench()
        report["duplicate"] = duplicate
        report["created"] = not duplicate
        return report

    apply = post
    add_record = post
    add_sorting_record = post
    record_sorting = post
    apply_sorting_record = post

    def commitment_for(self, lot_id):
        report = self.workbench()
        return next((row for row in report["batches"] if row["lot_id"] == lot_id), None)

    def ratio_for(self, lot_id):
        row = self.commitment_for(lot_id)
        return None if row is None else row["committed_ratio"]

    def ratios(self):
        return {row["lot_id"]: row["committed_ratio"] for row in self.workbench()["batches"]}

    def workbench(self) -> dict[str, Any]:
        return evaluate_certification(
            self.lots,
            records=list(self.records.values()),
            threshold=self.threshold,
        )

    evaluate = workbench
    recalculate = workbench


# 命名兼容：部分调用方可能把应用服务称作 CertificationService。
CertificationService = CertificationLedger
