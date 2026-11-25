from datetime import date, timedelta
from app.catalog import SPEC
from app.certification.engine import evaluate_certification
from app.domain.helpers import *  # noqa: F401,F403


_CERT_KEYS = {
    "certified_qty", "certified_quantity", "cert_ratio", "certified_ratio",
    "required_ratio", "threshold", "agreed_ratio", "sorting_records", "records",
}


def calculate_rows(items, sorting_records=None, records=None, threshold=None, **cert_kwargs):
    out = []
    rows = items
    lots = {item["lot_id"]: item for item in items}
    parents = {lot_id: item["contributions"] for lot_id, item in lots.items()}
    children = {lot_id: set() for lot_id in lots}
    used = {lot_id: 0 for lot_id in lots}
    indegree = {lot_id: 0 for lot_id in lots}
    for child, upstream in parents.items():
        for parent, quantity in upstream.items():
            if parent not in lots:
                lots[child]["problem"] = "PARENT_MISSING"
                continue
            children[parent].add(child)
            used[parent] += quantity
            indegree[child] += 1
    for parent, quantity in used.items():
        if quantity > lots[parent]["qty_out"]:
            lots[parent]["problem"] = "OVERDRAW"
            for child in children[parent]:
                lots[child]["problem"] = "OVERDRAW"
    queue = [lot_id for lot_id, count in indegree.items() if count == 0]
    while queue:
        parent = queue.pop()
        for child in children[parent]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    for lot_id, count in indegree.items():
        if count:
            lots[lot_id]["problem"] = "CYCLE_OR_DEPENDENT"

    recalled = {item["lot_id"] for item in items if item["recall"] == 1}
    affected = set(recalled)
    for graph in (parents, children):
        visited = set(recalled)
        queue = list(recalled)
        while queue:
            for neighbor in graph.get(queue.pop(), ()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        affected.update(visited)
    for item in items:
        item["affected"] = item["lot_id"] in affected
        item["breakdown"] = [f"{parent} · 投入 {quantity}" for parent, quantity in item["contributions"].items()]
        item["metric"] = D(str(item["qty_in"]), "CALC")
        item["limit"] = D(str(item["qty_out"]), "LIMIT")
        out.append(item)
    for item in out:
        item.setdefault("label", str(item.get(SPEC["key"], "")))

    has_cert_data = (
        sorting_records is not None or records is not None or threshold is not None
        or any(any(key in item for key in _CERT_KEYS) for item in out)
    )
    if has_cert_data:
        record_data = sorting_records if sorting_records is not None else records
        cert_report = evaluate_certification(
            out,
            records=record_data,
            threshold=threshold,
            **cert_kwargs,
        )
        cert_by_id = {row["lot_id"]: row for row in cert_report["batches"]}
        for item in out:
            cert = cert_by_id.get(str(item.get("lot_id", "")))
            if cert:
                item["certification"] = cert
                item["certification_status"] = cert["status"]
                item["certification_verdict"] = cert["verdict"]
                item["committed_ratio"] = cert["committed_ratio"]
                item["committed_ratio_text"] = cert["committed_ratio_text"]
                item["certification_basis"] = cert["basis"]
    return out


def calculate_certified_ratios(items, records=None, threshold=None, **kwargs):
    """兼容命名：直接返回认证承诺报告。"""
    return evaluate_certification(items, records=records, threshold=threshold, **kwargs)
