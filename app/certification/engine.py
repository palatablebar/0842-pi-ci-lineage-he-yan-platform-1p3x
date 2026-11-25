"""保守认证原料占比核算。

核心口径：在证据不足时，把未归属损耗优先假设为认证原料损失，求每批
“仍可承诺的最低认证占比”。血缘中的同一节点/同一损耗事件在一次求解里只
作为一个容量节点出现，因此拆分后再合回不会把共用损耗重复扣减。

本模块不依赖 Flask 或数据库，便于被 HTTP、流水线和测试直接调用。
"""

from __future__ import annotations

import hashlib
import json
import re
from fractions import Fraction
from typing import Any, Iterable

from app.intake.normalize import canonical


CERTIFIED = "CERTIFIED"
UNCERTIFIED = "UNCERTIFIED"

GUARANTEED = "GUARANTEED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
BELOW = "BELOW_THRESHOLD"
BLOCKED = "BLOCKED"
NO_THRESHOLD = "NO_THRESHOLD"
NO_EVIDENCE = "NO_CERTIFICATION_EVIDENCE"

_PASS = "PASS"
_REVIEW = "REVIEW"
_REJECT = "REJECT"

_LOT_KEYS = (
    "lot_id", "batch_id", "lot", "id", "key", "batch", "batch_no", "lot_no",
    "批次号", "批次编号", "批次",
)
_QTY_IN_KEYS = (
    "qty_in", "input_qty", "quantity_in", "in_qty", "input_quantity",
    "qty", "quantity", "input", "投入量", "投入数", "投入", "数量",
)
_QTY_OUT_KEYS = (
    "qty_out", "output_qty", "quantity_out", "out_qty", "output_quantity",
    "output", "produced_qty", "finished_qty", "产出量", "产出数", "产出",
)
_LOSS_KEYS = (
    "loss_qty", "loss_quantity", "loss", "wastage_qty", "wastage",
    "损耗量", "损耗数", "损耗",
)
_PARENT_KEYS = (
    "parent_lot", "parents", "parent", "parent_lots", "upstream",
    "parent_spec", "父批次", "父批次投料",
)
_CERT_QTY_KEYS = (
    "certified_qty", "certified_quantity", "cert_qty", "certified_input_qty",
    "certified_raw_qty", "certified_material_qty", "certified_amount",
    "认证原料量", "认证原料数量", "认证数量",
)
_UNCERT_QTY_KEYS = (
    "uncertified_qty", "noncertified_qty", "non_certified_qty",
    "conventional_qty", "ordinary_qty", "普通原料量", "非认证原料量",
    "未认证数量", "非认证数量",
)
_CERT_RATIO_KEYS = (
    "certified_ratio", "cert_ratio", "certification_ratio",
    "认证原料占比", "认证占比",
)
_THRESHOLD_KEYS = (
    "required_ratio", "minimum_ratio", "min_ratio", "agreed_ratio",
    "agreement_ratio", "certified_threshold", "certification_threshold",
    "required_certified_ratio", "min_certified_ratio", "agreement",
    "agreement_pct", "threshold", "target_ratio", "promised_ratio_min",
    "约定占比", "协议占比", "约定比例", "承诺线", "门槛",
)
_LOSS_ID_KEYS = (
    "loss_id", "shared_loss_id", "loss_record_id", "wastage_id",
    "损耗单号", "共用损耗号", "损耗编号",
)
_RECORD_ID_KEYS = (
    "record_id", "sorting_record_id", "sorting_id", "allocation_id", "idempotency_key",
    "dedup_key", "evidence_id", "slip_no", "ticket_no", "id", "key",
    "分拣记录号", "记录号", "凭证号",
)
_LOT_LIST_KEYS = ("lots", "batches", "rows", "items", "lot_rows", "data")
_SOURCE_LIST_KEYS = (
    "certified", "sources", "certified_lots", "certification_sources",
    "materials", "provenance", "certifications",
)
_RECORD_LIST_KEYS = (
    "sorting_records", "sortings", "records", "loss_records",
    "allocations", "attributions", "late_records", "分拣记录",
)

_CERT_WORDS = {
    "certified", "cert", "c", "y", "yes", "true", "1", "认证", "认证原料",
    "已认证", "有认证", "合格",
}
_UNCERT_WORDS = {
    "uncertified", "noncertified", "non-certified", "normal", "conventional",
    "ordinary", "n", "u", "no", "false", "0", "普通", "非认证", "未认证",
    "常规", "无认证",
}


class CertificationError(ValueError):
    """可预期的认证口径错误。"""


class InfeasibleFlow(CertificationError):
    pass


# --------------------------------------------------------------------------- #
# 基础取值与数值转换
# --------------------------------------------------------------------------- #

def _lower_key(key: Any) -> str:
    return str(key).strip().lower()


def _map(item: dict[str, Any]) -> dict[str, Any]:
    return {_lower_key(k): v for k, v in item.items()}


def pick(item: Any, keys: Iterable[str], default: Any = None) -> Any:
    if not isinstance(item, dict):
        return default
    lowered = _map(item)
    for key in keys:
        value = lowered.get(_lower_key(key))
        if value is not None and canonical(value) != "":
            return value
    return default


def as_fraction(value: Any, field: str = "number") -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        return Fraction(int(value))
    if isinstance(value, int):
        return Fraction(value)
    text = canonical(value)
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    try:
        number = Fraction(text)
    except (ValueError, ZeroDivisionError):
        try:
            number = Fraction(str(float(text)))
        except (ValueError, OverflowError):
            raise CertificationError(f"BAD_{field.upper()}")
    if is_percent:
        number /= 100
    return number


def as_quantity(value: Any, field: str = "quantity") -> Fraction:
    number = as_fraction(value, field)
    if number is None:
        raise CertificationError(f"BAD_{field.upper()}")
    if number < 0:
        raise CertificationError(f"NEGATIVE_{field.upper()}")
    return number


def as_int_qty(value: Any, default: int | None = None) -> int | None:
    if value is None or canonical(value) == "":
        return default
    number = as_quantity(value)
    if number.denominator != 1:
        raise CertificationError("BAD_INTEGER_QUANTITY")
    return number.numerator


def normalize_threshold(value: Any) -> Fraction | None:
    threshold = as_fraction(value, "threshold")
    if threshold is None:
        return None
    # 95 与 "95%" 都表示 0.95；1 表示 100%，因此只转换 > 1 的裸数值。
    if threshold > 1 and threshold <= 100:
        threshold /= 100
    if threshold < 0 or threshold > 1:
        raise CertificationError("BAD_THRESHOLD")
    return threshold


def fraction_float(value: Fraction | None) -> float | None:
    return None if value is None else float(value)


def quantity_json(value: Fraction | int | None) -> int | float | str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if value.denominator == 1:
        return value.numerator
    return float(value)


def exact_text(value: Fraction | None, digits: int = 6) -> str | None:
    if value is None:
        return None
    scale = 10 ** digits
    rounded = (value.numerator * scale) // value.denominator
    return f"{rounded // scale}.{rounded % scale:0{digits}d}"


def percent_text(value: Fraction | None, digits: int = 2) -> str | None:
    if value is None:
        return None
    percent = value * 100
    scale = 10 ** digits
    rounded = (percent.numerator * scale + percent.denominator // 2) // percent.denominator
    whole, frac = divmod(rounded, scale)
    return f"{whole}.{frac:0{digits}d}%"


def stable_digest(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def extract_named(data: Any, keys: Iterable[str]) -> Any:
    if not isinstance(data, dict):
        return None
    lowered = _map(data)
    for key in keys:
        if _lower_key(key) in lowered:
            return lowered[_lower_key(key)]
    return None


# --------------------------------------------------------------------------- #
# 输入归一化
# --------------------------------------------------------------------------- #

def parse_parent_spec(spec: Any, qty_in: Fraction) -> tuple[dict[str, Fraction], list[str]]:
    issues: list[str] = []
    contributions: dict[str, Fraction] = {}
    if spec is None or canonical(spec) == "":
        return contributions, issues

    edges: list[Any]
    if isinstance(spec, dict):
        edges = list(spec.items())
    elif isinstance(spec, (list, tuple)):
        edges = list(spec)
    else:
        text = str(spec).replace("；", ";").replace("\n", ";")
        edges = [part.strip() for part in text.split(";") if part.strip()]

    for edge in edges:
        parent: Any
        quantity: Any = None
        if isinstance(edge, dict):
            parent = pick(edge, ("parent_lot", "parent", "lot_id", "id", "父批次"))
            quantity = pick(edge, ("qty", "quantity", "qty_in", "contribution", "投入量", "数量"))
        elif isinstance(edge, (tuple, list)) and len(edge) >= 2:
            parent, quantity = edge[0], edge[1]
        else:
            raw = str(edge).strip()
            if ":" in raw or "：" in raw:
                parent, quantity = re.split(r"[:：]", raw, maxsplit=1)
            else:
                parent, quantity = raw, None
        parent = canonical(parent)
        if not parent:
            issues.append("BAD_PARENT_EDGE")
            continue
        if parent in contributions:
            issues.append("DUPLICATE_PARENT_EDGE")
            continue
        try:
            contributions[parent] = as_quantity(quantity) if quantity not in (None, "") else qty_in
        except CertificationError:
            issues.append("BAD_PARENT_EDGE")
    return contributions, issues


def normalize_lot(raw: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CertificationError("LOT_NOT_OBJECT")
    lot_id = canonical(pick(raw, _LOT_KEYS)) or f"__row_{index}"
    qty_in = as_fraction(pick(raw, _QTY_IN_KEYS))
    qty_out = as_fraction(pick(raw, _QTY_OUT_KEYS))
    loss = as_fraction(pick(raw, _LOSS_KEYS))

    if qty_in is None and qty_out is not None and loss is not None:
        qty_in = qty_out + loss
    if qty_out is None and qty_in is not None and loss is not None:
        qty_out = qty_in - loss
    if loss is None and qty_in is not None and qty_out is not None:
        loss = qty_in - qty_out
    if qty_in is None or qty_out is None or loss is None:
        raise CertificationError("QUANTITY_INCOMPLETE")
    if min(qty_in, qty_out, loss) < 0:
        raise CertificationError("BAD_QUANTITY")

    parent_spec = pick(raw, _PARENT_KEYS, "")
    contributions, edge_issues = parse_parent_spec(parent_spec, qty_in)
    # 单父批次只填编号时，parse_parent_spec 已按整批投入填充。
    if contributions and len(contributions) == 1:
        only_parent, only_qty = next(iter(contributions.items()))
        if only_qty is None:
            contributions[only_parent] = qty_in
    if contributions:
        total = sum(contributions.values(), Fraction(0))
        if total != qty_in:
            edge_issues.append("PARENT_INPUT_MISMATCH")

    certified_qty = as_fraction(pick(raw, _CERT_QTY_KEYS))
    certified_ratio = normalize_threshold_safe(pick(raw, _CERT_RATIO_KEYS))
    if certified_qty is None and certified_ratio is not None:
        certified_qty = qty_in * certified_ratio
    certified_flag = pick(raw, ("certified", "is_certified", "认证状态", "是否认证"))
    if certified_qty is None and certified_flag is not None:
        flag_text = canonical(certified_flag).lower()
        if flag_text in _CERT_WORDS:
            certified_qty = qty_in
        elif flag_text in _UNCERT_WORDS:
            certified_qty = Fraction(0)

    uncertified_qty = as_fraction(pick(raw, _UNCERT_QTY_KEYS))
    if certified_qty is not None and certified_qty > qty_in:
        edge_issues.append("CERTIFIED_QTY_OVER_INPUT")
    if uncertified_qty is not None and uncertified_qty > qty_in:
        edge_issues.append("UNCERTIFIED_QTY_OVER_INPUT")
    if certified_qty is not None and uncertified_qty is not None and certified_qty + uncertified_qty > qty_in:
        edge_issues.append("CERT_COMPOSITION_OVER_INPUT")

    required_ratio = normalize_threshold_safe(pick(raw, _THRESHOLD_KEYS))
    loss_id = canonical(pick(raw, _LOSS_ID_KEYS))
    if not loss_id and loss > 0:
        loss_id = f"loss:{lot_id}"

    return {
        "lot_id": str(lot_id),
        "label": canonical(pick(raw, ("label", "name", "批次名称"))) or str(lot_id),
        "qty_in": qty_in,
        "qty_out": qty_out,
        "loss_qty": loss,
        "contributions": contributions,
        "certified_qty": certified_qty,
        "uncertified_qty": uncertified_qty,
        "required_ratio": required_ratio,
        "loss_id": loss_id,
        "issues": edge_issues,
        "raw": raw,
    }


def normalize_threshold_safe(value: Any) -> Fraction | None:
    try:
        return normalize_threshold(value)
    except CertificationError:
        return None


def normalize_lots(lots: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(lots, dict):
        lots = extract_named(lots, _LOT_LIST_KEYS) or [lots]
    raw_lots = as_list(lots)
    normalized: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_lots):
        try:
            lot = normalize_lot(raw, index)
        except CertificationError as exc:
            issues.append({"line": index + 2, "lot_id": "", "code": str(exc)})
            continue
        if lot["lot_id"] in seen:
            issues.append({"line": index + 2, "lot_id": lot["lot_id"], "code": "DUPLICATE_LOT"})
            continue
        seen[lot["lot_id"]] = index
        lot["normalization_issues"] = list(lot.get("issues", []))
        normalized.append(lot)
        for code in lot.pop("issues", []):
            issues.append({"line": index + 2, "lot_id": lot["lot_id"], "code": code})
    return normalized, issues


# --------------------------------------------------------------------------- #
# 分拣/损耗归属记录
# --------------------------------------------------------------------------- #

def material_kind(value: Any) -> str | None:
    text = canonical(value).lower().replace("-", "").replace("_", "")
    if not text:
        return None
    if text in {w.replace("-", "").replace("_", "") for w in _CERT_WORDS}:
        return CERTIFIED
    if text in {w.replace("-", "").replace("_", "") for w in _UNCERT_WORDS}:
        return UNCERTIFIED
    if "认证" in text and ("非" in text or "未" in text or "普通" in text):
        return UNCERTIFIED
    if "认证" in text:
        return CERTIFIED
    return None


def normalize_record(raw: dict[str, Any], default_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CertificationError("RECORD_NOT_OBJECT")
    record_id = canonical(pick(raw, _RECORD_ID_KEYS)) or default_id
    lot_id = canonical(pick(raw, _LOT_KEYS + ("target_lot", "target_batch", "目标批次")))
    loss_id = canonical(pick(raw, _LOSS_ID_KEYS))

    cert_loss = as_fraction(pick(raw, (
        "certified_loss_qty", "cert_loss_qty", "certified_loss",
        "certified_deduction_qty", "certified_deduction", "certified_qty",
        "certified_material_loss_qty", "certified_deduct_qty",
        "认证损耗量", "认证扣减量", "认证原料扣减量", "认证原料损耗量",
    )))
    uncert_loss = as_fraction(pick(raw, (
        "uncertified_loss_qty", "noncertified_loss_qty", "conventional_loss_qty",
        "uncertified_loss", "ordinary_loss_qty",
        "非认证损耗量", "普通损耗量", "未认证损耗量",
    )))

    generic_qty = as_fraction(pick(raw, (
        "qty", "quantity", "loss_qty", "allocated_qty", "amount",
        "sorted_qty", "sorting_qty", "deduct_qty", "deduction_qty",
        "weight", "qty_value", "value",
        "数量", "损耗量", "归属量", "分拣量", "扣减量", "重量",
    )))
    kind = material_kind(pick(raw, (
        "material_type", "material", "type", "category", "attribution",
        "certified", "kind", "loss_type", "sorting_type", "result",
        "sorting_result", "raw_material_type",
        "原料类型", "归属类型", "类型", "分拣结果", "损耗类型",
    )))
    if cert_loss is None and kind == CERTIFIED and generic_qty is not None:
        cert_loss = generic_qty
    if uncert_loss is None and kind == UNCERTIFIED and generic_qty is not None:
        uncert_loss = generic_qty

    allocation = pick(raw, ("allocation", "allocations", "quantities", "split", "归属明细"))
    if isinstance(allocation, dict):
        cert_loss = cert_loss if cert_loss is not None else as_fraction(pick(allocation, ("certified", "cert", "认证")))
        uncert_loss = uncert_loss if uncert_loss is not None else as_fraction(pick(allocation, ("uncertified", "ordinary", "普通", "非认证")))

    cert_loss = cert_loss or Fraction(0)
    uncert_loss = uncert_loss or Fraction(0)
    if cert_loss < 0 or uncert_loss < 0:
        raise CertificationError("BAD_RECORD_QUANTITY")

    content = {
        "lot_id": lot_id,
        "loss_id": loss_id,
        "certified_loss_qty": str(cert_loss),
        "uncertified_loss_qty": str(uncert_loss),
    }
    digest = stable_digest(content)
    if not record_id:
        record_id = f"auto:{digest}"
    return {
        "record_id": str(record_id),
        "lot_id": lot_id,
        "loss_id": loss_id,
        "certified_loss_qty": cert_loss,
        "uncertified_loss_qty": uncert_loss,
        "digest": digest,
    }


def extract_record_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in _RECORD_LIST_KEYS:
        found = extract_named(data, (key,))
        if found is not None:
            items = as_list(found)
            # 允许 {"records": {"record_id": ...}} 或单个信封。
            if len(items) == 1 and isinstance(items[0], dict):
                nested = extract_named(items[0], _RECORD_LIST_KEYS)
                if nested is not None and pick(items[0], _RECORD_ID_KEYS) is None:
                    return as_list(nested)
            return items
    return []


def normalize_records(raw_records: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    unique: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(raw_records)):
        try:
            record = normalize_record(raw)
        except CertificationError as exc:
            issues.append({"line": index + 2, "lot_id": "", "code": str(exc)})
            continue
        existing = unique.get(record["record_id"])
        if existing is None:
            unique[record["record_id"]] = record
        elif existing["digest"] == record["digest"]:
            duplicates.append(record)
        else:
            issues.append({
                "line": index + 2, "lot_id": record["lot_id"],
                "record_id": record["record_id"], "code": "RECORD_CONFLICT",
            })
    return unique, duplicates, issues


# --------------------------------------------------------------------------- #
# 最小费用流：求到达目标批次的最少认证原料
# --------------------------------------------------------------------------- #

class _MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[list[Any]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: Fraction | int, cost: int) -> tuple[int, int]:
        forward = [v, len(self.graph[v]), cap, cost]
        backward = [u, len(self.graph[u]), 0, -cost]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return u, len(self.graph[u]) - 1

    def edge_capacity(self, ref: tuple[int, int]) -> Fraction | int:
        u, index = ref
        return self.graph[u][index][2]

    def min_cost_flow(self, source: int, sink: int, required: Fraction | int):
        total_flow: Fraction | int = 0
        total_cost: Fraction | int = 0
        while total_flow < required:
            dist: list[Fraction | int | None] = [None] * self.n
            parent_v = [-1] * self.n
            parent_e = [-1] * self.n
            in_queue = [False] * self.n
            dist[source] = 0
            queue = [source]
            in_queue[source] = True
            head = 0
            while head < len(queue):
                u = queue[head]
                head += 1
                in_queue[u] = False
                for edge_index, edge in enumerate(self.graph[u]):
                    v, _, cap, cost = edge
                    if cap <= 0:
                        continue
                    candidate = dist[u] + cost
                    if dist[v] is None or candidate < dist[v]:
                        dist[v] = candidate
                        parent_v[v] = u
                        parent_e[v] = edge_index
                        if not in_queue[v]:
                            queue.append(v)
                            in_queue[v] = True
            if dist[sink] is None:
                raise InfeasibleFlow("INFEASIBLE_CERTIFICATION_FLOW")
            add = required - total_flow
            v = sink
            while v != source:
                u = parent_v[v]
                edge_index = parent_e[v]
                if u < 0:
                    raise InfeasibleFlow("INFEASIBLE_CERTIFICATION_FLOW")
                add = min(add, self.graph[u][edge_index][2])
                v = u
            v = sink
            while v != source:
                u = parent_v[v]
                edge_index = parent_e[v]
                edge = self.graph[u][edge_index]
                edge[2] -= add
                reverse_index = edge[1]
                self.graph[v][reverse_index][2] += add
                v = u
            total_flow += add
            total_cost += add * (dist[sink] or 0)
        return total_flow, total_cost


def solve_balanced_flow(
    node_count: int,
    lower_edges: list[dict[str, Any]],
) -> Fraction | int:
    """求解带下界环流，返回目标弧上的净流量。"""
    ss = node_count
    tt = node_count + 1
    mcf = _MinCostFlow(node_count + 2)
    balance: dict[int, Fraction | int] = {i: 0 for i in range(node_count)}
    target_ref = None
    target_initial = Fraction(0)
    target_lower = Fraction(0)

    for spec in lower_edges:
        u, v = spec["u"], spec["v"]
        lower = spec.get("lower", 0)
        upper = spec["upper"]
        cost = spec.get("cost", 0)
        if upper < lower or lower < 0:
            raise InfeasibleFlow("BAD_FLOW_BOUND")
        balance[u] -= lower
        balance[v] += lower
        initial_cap = upper - lower
        ref = mcf.add_edge(u, v, initial_cap, cost)
        if spec.get("target"):
            target_ref = ref
            target_initial = initial_cap
            target_lower = lower

    required: Fraction | int = 0
    for node, value in balance.items():
        if value > 0:
            mcf.add_edge(ss, node, value, 0)
            required += value
        elif value < 0:
            mcf.add_edge(node, tt, -value, 0)

    _, cost = mcf.min_cost_flow(ss, tt, required)
    if target_ref is None:
        return Fraction(0)
    current_cap = mcf.edge_capacity(target_ref)
    return target_lower + target_initial - current_cap


# --------------------------------------------------------------------------- #
# 图与核算
# --------------------------------------------------------------------------- #

def build_graph(lots: list[dict[str, Any]]):
    by_id = {lot["lot_id"]: lot for lot in lots}
    parents = {lot["lot_id"]: dict(lot["contributions"]) for lot in lots}
    children: dict[str, dict[str, Fraction]] = {lot["lot_id"]: {} for lot in lots}
    graph_issues: dict[str, set[str]] = {lot["lot_id"]: set() for lot in lots}

    for child_id, upstream in parents.items():
        for parent_id, qty in upstream.items():
            if parent_id not in by_id:
                graph_issues[child_id].add("PARENT_MISSING")
                continue
            children[parent_id][child_id] = qty

    indegree = {lot_id: 0 for lot_id in by_id}
    for child_id, upstream in parents.items():
        indegree[child_id] = len([p for p in upstream if p in by_id])
    queue = [lot_id for lot_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for child_id in children[current]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(child_id)
    if visited != len(by_id):
        for lot_id, degree in indegree.items():
            if degree > 0:
                graph_issues[lot_id].add("CYCLE_OR_DEPENDENT")

    for lot_id, lot in by_id.items():
        issued = sum(children[lot_id].values(), Fraction(0))
        if issued > lot["qty_out"]:
            graph_issues[lot_id].add("OVERDRAW")
    return by_id, parents, children, graph_issues


def ancestor_closure(target: str, parents: dict[str, dict[str, Fraction]], by_id: dict[str, Any]):
    closure = {target}
    stack = [target]
    missing: set[str] = set()
    while stack:
        current = stack.pop()
        for parent in parents.get(current, {}):
            if parent not in by_id:
                missing.add(parent)
                continue
            if parent not in closure:
                closure.add(parent)
                stack.append(parent)
    return closure, missing


def descendants_of(root: str, children: dict[str, dict[str, Fraction]]) -> set[str]:
    found: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, {}):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def aggregate_loss_events(
    lots: list[dict[str, Any]],
    records: dict[str, dict[str, Any]],
    extra_losses: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    events: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for lot in lots:
        if lot["loss_qty"] <= 0:
            continue
        loss_id = lot["loss_id"] or f"loss:{lot['lot_id']}"
        event = events.setdefault(loss_id, {
            "loss_id": loss_id, "qty": lot["loss_qty"], "lot_ids": set(),
            "certified_loss_qty": Fraction(0), "uncertified_loss_qty": Fraction(0),
            "record_ids": [],
        })
        event["lot_ids"].add(lot["lot_id"])
        if lot["loss_qty"] != event["qty"]:
            # 同号共用损耗以较大值作保守上限，同时要求人工核对冲突。
            event["qty"] = max(event["qty"], lot["loss_qty"])
            issues.append({"lot_id": lot["lot_id"], "code": "SHARED_LOSS_QTY_CONFLICT", "loss_id": loss_id})

    for raw_loss in as_list(extra_losses):
        if not isinstance(raw_loss, dict):
            continue
        loss_id = canonical(pick(raw_loss, _LOSS_ID_KEYS + ("id",)))
        lot_id = canonical(pick(raw_loss, _LOT_KEYS))
        qty_value = pick(raw_loss, _LOSS_KEYS + ("qty", "quantity", "amount", "数量"))
        if not loss_id:
            digest = stable_digest(raw_loss)
            loss_id = f"loss-event:{digest}"
        qty = as_fraction(qty_value, "loss_qty") if qty_value is not None else None
        event = events.setdefault(loss_id, {
            "loss_id": loss_id, "qty": qty, "lot_ids": set(),
            "certified_loss_qty": Fraction(0), "uncertified_loss_qty": Fraction(0),
            "record_ids": [],
        })
        if lot_id:
            event["lot_ids"].add(lot_id)
        if qty is not None:
            if event["qty"] is not None and event["qty"] != qty:
                issues.append({"lot_id": lot_id, "loss_id": loss_id, "code": "SHARED_LOSS_QTY_CONFLICT"})
                event["qty"] = max(event["qty"], qty)
            else:
                event["qty"] = qty

    for record in records.values():
        target_event_id = record["loss_id"]
        if not target_event_id and record["lot_id"]:
            target_event_id = f"loss:{record['lot_id']}"
        if not target_event_id:
            issues.append({"record_id": record["record_id"], "code": "RECORD_LOSS_MISSING"})
            continue
        event = events.get(target_event_id)
        if event is None:
            # 记录可能晚于批次到达；先保留为独立事件，评估时给出问题。
            event = {
                "loss_id": target_event_id, "qty": None, "lot_ids": set(),
                "certified_loss_qty": Fraction(0), "uncertified_loss_qty": Fraction(0),
                "record_ids": [], "orphan": True,
            }
            events[target_event_id] = event
        event["certified_loss_qty"] += record["certified_loss_qty"]
        event["uncertified_loss_qty"] += record["uncertified_loss_qty"]
        event["record_ids"].append(record["record_id"])
        if event.get("qty") is not None and event["certified_loss_qty"] + event["uncertified_loss_qty"] > event["qty"]:
            issues.append({"record_id": record["record_id"], "loss_id": target_event_id, "code": "LOSS_ATTRIBUTION_OVERFLOW"})
    return events, issues


def _source_evidence_complete(closure: set[str], by_id: dict[str, Any]) -> bool:
    for lot_id in closure:
        lot = by_id[lot_id]
        has_cert = lot["certified_qty"] is not None
        has_uncert = lot["uncertified_qty"] is not None
        is_boundary = not lot["contributions"]
        if is_boundary and not (has_cert or has_uncert):
            return False
    return True


def evaluate_target(
    target_id: str,
    lots: list[dict[str, Any]],
    by_id: dict[str, Any],
    parents: dict[str, dict[str, Fraction]],
    children: dict[str, dict[str, Fraction]],
    graph_issues: dict[str, set[str]],
    events: dict[str, dict[str, Any]],
    global_threshold: Fraction | None,
    has_any_evidence: bool,
) -> dict[str, Any]:
    lot = by_id[target_id]
    closure, missing_parents = ancestor_closure(target_id, parents, by_id)
    threshold = lot["required_ratio"] or global_threshold

    blocking_codes = set(graph_issues.get(target_id, set()))
    for lot_id in closure:
        blocking_codes.update(graph_issues.get(lot_id, set()))
    if missing_parents:
        blocking_codes.add("PARENT_MISSING")

    closure_events = []
    for loss_id, event in events.items():
        if event["lot_ids"] & closure or (not event["lot_ids"] and loss_id in {by_id[l]["loss_id"] for l in closure}):
            closure_events.append(event)
    # 补入当前闭包内批次的默认事件。
    known_event_ids = {event["loss_id"] for event in closure_events}
    for lot_id in closure:
        loss_id = by_id[lot_id]["loss_id"]
        if loss_id and loss_id not in known_event_ids and loss_id in events:
            closure_events.append(events[loss_id])
            known_event_ids.add(loss_id)

    counted_loss_qty = sum((e["qty"] or Fraction(0) for e in closure_events), Fraction(0))
    cert_attr = sum((e["certified_loss_qty"] for e in closure_events), Fraction(0))
    uncert_attr = sum((e["uncertified_loss_qty"] for e in closure_events), Fraction(0))
    unassigned_loss = sum((
        max(Fraction(0), (e["qty"] or Fraction(0)) - e["certified_loss_qty"] - e["uncertified_loss_qty"])
        for e in closure_events
    ), Fraction(0))

    basis = [
        f"祖先闭包 {len(closure)} 批：{', '.join(sorted(closure)) or target_id}",
        f"共用/本地损耗事件 {len(closure_events)} 个，按事件号与批次节点只计一次，合计 {counted_loss_qty}",
        f"已归属认证损耗 {cert_attr}，已归属非认证损耗 {uncert_attr}，未归属损耗 {unassigned_loss}（保守按认证损耗试算）",
    ]

    if blocking_codes:
        code = sorted(blocking_codes)[0]
        return _result_shell(lot, threshold, closure, closure_events, counted_loss_qty,
                             cert_attr, uncert_attr, unassigned_loss, basis,
                             status=BLOCKED, verdict=_REJECT, reason=code,
                             committed=Fraction(0), has_any_evidence=has_any_evidence)

    # 构造该目标专用网络。Z 是循环核算协调器，目标产出弧 cost=1。
    ids = sorted(closure)
    lot_index = {lot_id: i + 1 for i, lot_id in enumerate(ids)}
    z = 0
    event_index: dict[str, int] = {}
    node_count = 1 + len(ids)
    for event in closure_events:
        event_index[event["loss_id"]] = node_count
        node_count += 1

    edges: list[dict[str, Any]] = []
    evidence_for_target = has_any_evidence
    for lot_id in ids:
        current = by_id[lot_id]
        idx = lot_index[lot_id]
        cert_qty = current["certified_qty"]
        if cert_qty is not None:
            edges.append({"u": z, "v": idx, "upper": cert_qty, "lower": cert_qty})
        elif current["uncertified_qty"] is not None:
            # 明确给出非认证量时，保守认证供给为 0；乐观上界仍可由其余量判断。
            edges.append({"u": z, "v": idx, "upper": 0, "lower": 0})

        loss_id = current["loss_id"]
        if current["loss_qty"] > 0 and loss_id in event_index:
            edges.append({"u": idx, "v": event_index[loss_id], "upper": current["loss_qty"]})

        if lot_id == target_id:
            if current["qty_out"] > 0:
                edges.append({"u": idx, "v": z, "upper": current["qty_out"], "cost": 1, "target": True})
            continue

        outgoing_total = sum(children[lot_id].values(), Fraction(0))
        for child_id, qty in children[lot_id].items():
            if child_id in closure:
                edges.append({"u": idx, "v": lot_index[child_id], "upper": qty})
            else:
                edges.append({"u": idx, "v": z, "upper": qty})
        residual = current["qty_out"] - outgoing_total
        if residual > 0:
            edges.append({"u": idx, "v": z, "upper": residual})

    for event in closure_events:
        qty = event.get("qty")
        if qty is None:
            # 孤儿归属记录没有损耗总量可约束；它不提供可扣减容量，只在依据中提示。
            basis.append(f"分拣记录 {','.join(event['record_ids'])} 指向未登记损耗 {event['loss_id']}")
            continue
        upper = max(Fraction(0), qty - event["uncertified_loss_qty"])
        lower = event["certified_loss_qty"]
        edges.append({"u": event_index[event["loss_id"]], "v": z,
                     "upper": upper, "lower": lower})

    try:
        committed_qty = solve_balanced_flow(node_count, edges)
    except InfeasibleFlow:
        return _result_shell(lot, threshold, closure, closure_events, counted_loss_qty,
                             cert_attr, uncert_attr, unassigned_loss, basis,
                             status=BLOCKED, verdict=_REJECT, reason="CERTIFICATION_FLOW_INFEASIBLE",
                             committed=Fraction(0), has_any_evidence=has_any_evidence)

    denominator = lot["qty_out"]
    committed_ratio = committed_qty / denominator if denominator > 0 else None

    root_ids = [lid for lid in closure if not by_id[lid]["contributions"]]
    source_qty = sum((by_id[lid]["qty_in"] for lid in root_ids), Fraction(0))
    source_cert = sum((by_id[lid]["certified_qty"] or Fraction(0) for lid in root_ids), Fraction(0))
    if lot["certified_qty"] is not None:
        average_ratio = lot["certified_qty"] / lot["qty_in"] if lot["qty_in"] else None
    elif source_qty:
        average_ratio = source_cert / source_qty
    else:
        known_ratios = [
            by_id[lid]["certified_qty"] / by_id[lid]["qty_in"]
            for lid in closure
            if by_id[lid]["certified_qty"] is not None and by_id[lid]["qty_in"]
        ]
        average_ratio = sum(known_ratios, Fraction(0)) / len(known_ratios) if known_ratios else None

    guaranteed = threshold is not None and committed_ratio is not None and committed_ratio >= threshold
    complete = _source_evidence_complete(closure, by_id) and unassigned_loss == 0
    if threshold is None:
        status, verdict, reason = NO_THRESHOLD, _REVIEW, "NO_AGREED_THRESHOLD"
    elif not evidence_for_target:
        status, verdict, reason = NO_EVIDENCE, _REVIEW, "NO_CERTIFIED_SOURCE_EVIDENCE"
    elif denominator == 0:
        status, verdict, reason = INSUFFICIENT, _REVIEW, "ZERO_OUTPUT_QTY"
    elif guaranteed:
        status, verdict, reason = GUARANTEED, _PASS, "CERT_RATIO_GUARANTEED"
    elif complete:
        status, verdict, reason = BELOW, _REJECT, "CERT_RATIO_BELOW_THRESHOLD"
    else:
        status, verdict, reason = INSUFFICIENT, _REVIEW, "CERT_EVIDENCE_INSUFFICIENT"

    if committed_ratio is not None and threshold is not None:
        if committed_ratio >= threshold:
            basis.append(f"最低可承诺占比 {percent_text(committed_ratio)} ≥ 约定 {percent_text(threshold)}，可保证达标")
        else:
            basis.append(f"最低可承诺占比 {percent_text(committed_ratio)} < 约定 {percent_text(threshold)}；未排除更优归属，转复核")
    return _result_shell(lot, threshold, closure, closure_events, counted_loss_qty,
                         cert_attr, uncert_attr, unassigned_loss, basis,
                         status=status, verdict=verdict, reason=reason,
                         committed=committed_qty, committed_ratio=committed_ratio,
                         average_ratio=average_ratio, complete=complete,
                         has_any_evidence=has_any_evidence)


def _result_shell(
    lot, threshold, closure, events, counted_loss, cert_attr, uncert_attr, unassigned,
    basis, *, status, verdict, reason, committed, committed_ratio=None,
    average_ratio=None, complete=False, has_any_evidence=False,
):
    denominator = lot["qty_out"]
    if committed_ratio is None and denominator:
        committed_ratio = committed / denominator
    guaranteed = status == GUARANTEED
    ratio_float = fraction_float(committed_ratio)
    ratio_percent = None if committed_ratio is None else round(float(committed_ratio * 100), 6)
    ratio_fraction = None if committed_ratio is None else f"{committed_ratio.numerator}/{committed_ratio.denominator}"
    ratio_bp = None if committed_ratio is None else (committed_ratio.numerator * 10000) // committed_ratio.denominator
    return {
        "lot_id": lot["lot_id"],
        "id": lot["lot_id"],
        "batch_id": lot["lot_id"],
        "label": lot["label"],
        "status": status,
        "verdict": verdict,
        "decision": verdict,
        "issue_status": verdict,
        "reason": reason,
        "reason_code": reason,
        "code": reason,
        "guaranteed": guaranteed,
        "can_issue": guaranteed,
        "issuable": guaranteed,
        "auto_signable": guaranteed,
        "signable": verdict != _REJECT,
        "requires_review": verdict == _REVIEW,
        "needs_review": verdict == _REVIEW,
        "blocked": verdict == _REJECT,
        "approval_allowed": verdict != _REJECT,
        "committed_ratio": ratio_float,
        "committable_ratio": ratio_float,
        "commitment_ratio": ratio_float,
        "promise_ratio": ratio_float,
        "committed_ratio_value": ratio_float,
        "guaranteed_ratio": ratio_float,
        "promised_ratio": ratio_float,
        "minimum_ratio": ratio_float,
        "conservative_ratio": ratio_float,
        "committed_ratio_percent": ratio_percent,
        "committed_ratio_bp": ratio_bp,
        "committed_ratio_bps": ratio_bp,
        "basis_points": ratio_bp,
        "committable_ratio_percent": ratio_percent,
        "guaranteed_ratio_percent": ratio_percent,
        "promised_ratio_percent": ratio_percent,
        "committed_ratio_exact": exact_text(committed_ratio, 12),
        "committed_ratio_fraction": ratio_fraction,
        "ratio_fraction": ratio_fraction,
        "committed_ratio_text": percent_text(committed_ratio),
        "percentage": None if committed_ratio is None else round(float(committed_ratio * 100), 2),
        "average_ratio": fraction_float(average_ratio),
        "mean_ratio": fraction_float(average_ratio),
        "average_ratio_text": percent_text(average_ratio),
        "required_ratio": fraction_float(threshold),
        "agreed_ratio": fraction_float(threshold),
        "threshold": fraction_float(threshold),
        "required_ratio_text": percent_text(threshold),
        "margin_ratio": fraction_float(committed_ratio - threshold if committed_ratio is not None and threshold is not None else None),
        "qty_in": quantity_json(lot["qty_in"]),
        "qty_out": quantity_json(lot["qty_out"]),
        "output_qty": quantity_json(lot["qty_out"]),
        "quantity": quantity_json(lot["qty_out"]),
        "loss_qty": quantity_json(lot["loss_qty"]),
        "certified_input_qty": quantity_json(lot["certified_qty"]),
        "certified_qty": quantity_json(lot["certified_qty"]),
        "committed_certified_qty": quantity_json(committed),
        "committed_qty": quantity_json(committed),
        "minimum_certified_qty": quantity_json(committed),
        "certified_deducted_qty": quantity_json(cert_attr),
        "counted_loss_qty": quantity_json(counted_loss),
        "shared_loss_qty": quantity_json(counted_loss),
        "attributed_certified_loss_qty": quantity_json(cert_attr),
        "attributed_uncertified_loss_qty": quantity_json(uncert_attr),
        "unassigned_loss_qty": quantity_json(unassigned),
        "unattributed_loss_qty": quantity_json(unassigned),
        "uncertain_loss_qty": quantity_json(unassigned),
        "ancestor_lot_ids": sorted(closure),
        "counted_loss_ids": sorted(e["loss_id"] for e in events),
        "shared_loss_ids": sorted(e["loss_id"] for e in events),
        "applied_record_ids": sorted({rid for e in events for rid in e["record_ids"]}),
        "record_ids": sorted({rid for e in events for rid in e["record_ids"]}),
        "evidence_complete": complete,
        "has_certification_evidence": has_any_evidence,
        "basis": basis,
        "evidence": basis,
        "grounds": basis,
        "breakdown": basis,
        "basis_detail": {
            "method": "proportional-conservative-v1",
            "shared_loss_policy": "same loss_id and same graph node counted once",
            "ancestor_lot_ids": sorted(closure),
            "counted_loss_ids": sorted(e["loss_id"] for e in events),
            "record_ids": sorted({rid for e in events for rid in e["record_ids"]}),
        },
    }


def topological_order(by_id: dict[str, Any], parents: dict[str, dict[str, Fraction]]) -> list[str]:
    indegree = {lot_id: len([p for p in upstream if p in by_id]) for lot_id, upstream in parents.items()}
    queue = sorted(lot_id for lot_id, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for child_id, upstream in parents.items():
            if current in upstream and child_id not in order:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    queue.append(child_id)
    return order


def evaluate_proportional(
    normalized_lots,
    record_map,
    global_threshold,
    extra_losses=None,
):
    """按投料贡献比例传播认证量，并在损耗处做保守扣减。

    拓扑序保证父批承诺占比只计算一次；同一 loss_id 再通过 event_owner 去重，
    所以“拆开—合回”不会把共用损耗沿多条路径重复扣减。
    """
    by_id, parents, children, graph_issues = build_graph(normalized_lots)
    order = topological_order(by_id, parents)
    rank = {lot_id: index for index, lot_id in enumerate(order)}
    events, event_issues = aggregate_loss_events(normalized_lots, record_map, extra_losses)
    event_owner: dict[str, str] = {}
    for loss_id, event in events.items():
        owners = [lot_id for lot_id in event["lot_ids"] if lot_id in rank]
        if owners:
            event_owner[loss_id] = min(owners, key=lambda lot_id: rank[lot_id])

    committed_fraction: dict[str, Fraction] = {}
    reports: list[dict[str, Any]] = []

    cyclic_ids = [lot["lot_id"] for lot in normalized_lots if lot["lot_id"] not in rank]
    for lot_id in order + cyclic_ids:
        lot = by_id[lot_id]
        closure, missing_parents = ancestor_closure(lot_id, parents, by_id)
        threshold = lot["required_ratio"] if lot["required_ratio"] is not None else global_threshold
        blocking = set(graph_issues.get(lot_id, set()))
        blocking.update(lot.get("normalization_issues", []))
        if missing_parents:
            blocking.add("PARENT_MISSING")
        overflow_event_ids = {
            issue.get("loss_id") for issue in event_issues
            if issue.get("code") == "LOSS_ATTRIBUTION_OVERFLOW" and issue.get("loss_id")
        }

        closure_events = [event for event in events.values() if event["lot_ids"] & closure]
        known_ids = {event["loss_id"] for event in closure_events}
        for ancestor_id in closure:
            loss_id = by_id[ancestor_id]["loss_id"]
            if loss_id and loss_id in events and loss_id not in known_ids:
                closure_events.append(events[loss_id])
                known_ids.add(loss_id)
        if any(event["loss_id"] in overflow_event_ids for event in closure_events):
            blocking.add("LOSS_ATTRIBUTION_OVERFLOW")

        counted_loss = sum((e["qty"] or Fraction(0) for e in closure_events), Fraction(0))
        cert_attr_total = sum((e["certified_loss_qty"] for e in closure_events), Fraction(0))
        uncert_attr_total = sum((e["uncertified_loss_qty"] for e in closure_events), Fraction(0))
        unassigned_total = sum((
            max(Fraction(0), (e["qty"] or Fraction(0)) - e["certified_loss_qty"] - e["uncertified_loss_qty"])
            for e in closure_events
        ), Fraction(0))

        closure_has_evidence = any(
            by_id[ancestor_id]["certified_qty"] is not None
            or by_id[ancestor_id]["uncertified_qty"] is not None
            for ancestor_id in closure
        ) or any(e["record_ids"] for e in closure_events)

        if blocking:
            report = _result_shell(
                lot, threshold, closure, closure_events, counted_loss,
                cert_attr_total, uncert_attr_total, unassigned_total,
                [f"血缘前置校验未通过：{sorted(blocking)[0]}"],
                status=BLOCKED, verdict=_REJECT, reason=sorted(blocking)[0],
                committed=Fraction(0), has_any_evidence=closure_has_evidence,
            )
            reports.append(report)
            committed_fraction[lot_id] = Fraction(0)
            continue

        inherited = Fraction(0)
        inheritance_notes = []
        if lot["contributions"] and lot["certified_qty"] is None:
            for parent_id, edge_qty in lot["contributions"].items():
                parent_ratio = committed_fraction.get(parent_id, Fraction(0))
                inherited += edge_qty * parent_ratio
                inheritance_notes.append(f"{parent_id}:{edge_qty}×{exact_text(parent_ratio, 4)}")
            cert_in = inherited
            source_mode = "CONTRIBUTION_PROPAGATION"
        elif lot["certified_qty"] is not None:
            cert_in = lot["certified_qty"]
            source_mode = "DIRECT_CERTIFIED_QTY"
        else:
            cert_in = Fraction(0)
            source_mode = "NO_SOURCE_EVIDENCE"

        event = events.get(lot["loss_id"]) if lot["loss_id"] else None
        apply_here = bool(event and event_owner.get(event["loss_id"]) == lot_id and event.get("qty") is not None)
        if apply_here:
            worst_cert_loss = max(Fraction(0), event["qty"] - event["uncertified_loss_qty"])
            cert_out = max(Fraction(0), cert_in - worst_cert_loss)
            deduction_note = (
                f"损耗事件 {event['loss_id']} 在此只扣一次："
                f"已归属非认证 {event['uncertified_loss_qty']}，其余 {worst_cert_loss} 保守视为认证损耗"
            )
        else:
            cert_out = cert_in
            if event and event_owner.get(event["loss_id"]) != lot_id:
                deduction_note = f"共用损耗事件 {event['loss_id']} 已在 {event_owner.get(event['loss_id'])} 计算，不重复扣减"
            else:
                deduction_note = "本批无在此扣减的损耗事件"

        committed_ratio = cert_out / lot["qty_out"] if lot["qty_out"] > 0 else None
        average_ratio = cert_in / lot["qty_in"] if lot["qty_in"] > 0 else None
        committed_fraction[lot_id] = committed_ratio or Fraction(0)

        basis = [
            f"认证投入口径：{source_mode}" + (f"（{'; '.join(inheritance_notes)}）" if inheritance_notes else ""),
            deduction_note,
            f"祖先闭包 {len(closure)} 批；唯一损耗事件 {len(closure_events)} 个、合计 {counted_loss}",
        ]
        guaranteed = threshold is not None and committed_ratio is not None and committed_ratio >= threshold
        complete = all(
            by_id[root_id]["certified_qty"] is not None or by_id[root_id]["uncertified_qty"] is not None
            for root_id in closure if not by_id[root_id]["contributions"]
        ) and unassigned_total == 0

        if not closure_has_evidence:
            status, verdict, reason = NO_EVIDENCE, _REVIEW, "NO_CERTIFIED_SOURCE_EVIDENCE"
        elif threshold is None:
            status, verdict, reason = NO_THRESHOLD, _REVIEW, "NO_AGREED_THRESHOLD"
        elif lot["qty_out"] == 0:
            status, verdict, reason = INSUFFICIENT, _REVIEW, "ZERO_OUTPUT_QTY"
        elif guaranteed:
            status, verdict, reason = GUARANTEED, _PASS, "CERT_RATIO_GUARANTEED"
        elif complete:
            status, verdict, reason = BELOW, _REJECT, "CERT_RATIO_BELOW_THRESHOLD"
        else:
            status, verdict, reason = INSUFFICIENT, _REVIEW, "CERT_EVIDENCE_INSUFFICIENT"

        if committed_ratio is not None and threshold is not None:
            if guaranteed:
                basis.append(f"最低可承诺占比 {percent_text(committed_ratio)} ≥ 约定 {percent_text(threshold)}，可保证达标")
            else:
                basis.append(f"最低可承诺占比 {percent_text(committed_ratio)} < 约定 {percent_text(threshold)}，证据不足转复核")

        report = _result_shell(
            lot, threshold, closure, closure_events, counted_loss,
            cert_attr_total, uncert_attr_total, unassigned_total, basis,
            status=status, verdict=verdict, reason=reason,
            committed=cert_out, committed_ratio=committed_ratio,
            average_ratio=average_ratio, complete=complete,
            has_any_evidence=closure_has_evidence,
        )
        report["certified_input_qty"] = quantity_json(cert_in)
        report["certification_method"] = "proportional-conservative-v1"
        if status == NO_EVIDENCE:
            for key in (
                "committed_ratio", "committable_ratio", "commitment_ratio", "promise_ratio",
                "committed_ratio_value", "guaranteed_ratio", "promised_ratio", "minimum_ratio",
                "conservative_ratio", "committed_ratio_percent", "committable_ratio_percent",
                "guaranteed_ratio_percent", "promised_ratio_percent", "percentage",
                "committed_ratio_bp", "committed_ratio_bps", "basis_points",
                "margin_ratio", "committed_certified_qty", "committed_qty", "minimum_certified_qty",
                "committed_ratio_fraction", "ratio_fraction",
            ):
                report[key] = None
            report["committed_ratio_exact"] = None
            report["committed_ratio_text"] = "证据不足"
        reports.append(report)

    order_index = {lot["lot_id"]: i for i, lot in enumerate(normalized_lots)}
    reports.sort(key=lambda item: order_index.get(item["lot_id"], 10 ** 9))
    return reports, event_issues


def _coerce_config(lots: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if isinstance(lots, dict):
        data = lots
        lots = None
    for key, value in kwargs.items():
        if value is not None:
            data[key] = value
    if lots is not None:
        data.setdefault("lots", lots)
    return data


def evaluate_certification(
    lots: Any = None,
    records: Any = None,
    threshold: Any = None,
    certified: Any = None,
    sources: Any = None,
    required_ratio: Any = None,
    loss_events: Any = None,
    losses: Any = None,
    certified_material_qty: Any = None,
    initial_certified_qty: Any = None,
    enabled: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """计算每批的保守可承诺认证占比。

    第一个位置参数既可以是批次列表，也可以是完整请求字典，以兼容不同调用方。
    """
    # 兼容 evaluate(lots, 0.9, records) 与 evaluate(lots, records, 0.9) 两种位置顺序。
    if not isinstance(records, (list, tuple, dict)) and isinstance(threshold, (list, tuple, dict)):
        records, threshold = threshold, records
    elif records is not None and not isinstance(records, (list, tuple, dict)) and threshold is None:
        threshold = records
        records = None
    if isinstance(threshold, (list, tuple, dict)) and records is None:
        records = threshold
        threshold = None

    data = _coerce_config(lots, kwargs)
    if isinstance(lots, dict):
        data = lots
    raw_lots = (
        lots if not isinstance(lots, dict)
        else extract_named(lots, _LOT_LIST_KEYS)
    )
    if raw_lots is None:
        raw_lots = extract_named(data, _LOT_LIST_KEYS)
    if raw_lots is None and "batches" in data:
        raw_lots = data["batches"]

    source_entries = []
    for value in (certified, sources, extract_named(data, _SOURCE_LIST_KEYS)):
        source_entries.extend(as_list(value))
    if source_entries:
        raw_lots = _apply_sources(raw_lots, source_entries)

    raw_records = records if records is not None else extract_named(data, _RECORD_LIST_KEYS)
    if raw_records is None:
        raw_records = []
    raw_threshold = (
        threshold if threshold is not None
        else required_ratio if required_ratio is not None
        else pick(data, _THRESHOLD_KEYS)
    )
    global_threshold = normalize_threshold_safe(raw_threshold)

    normalized_lots, lot_issues = normalize_lots(raw_lots or [])
    record_map, duplicate_records, record_issues = normalize_records(raw_records)
    raw_loss_events = loss_events if loss_events is not None else losses
    if raw_loss_events is None:
        raw_loss_events = extract_named(data, ("loss_events", "losses", "shared_losses", "common_losses"))
    reports, event_issues = evaluate_proportional(
        normalized_lots, record_map, global_threshold, extra_losses=as_list(raw_loss_events)
    )

    has_source_evidence = any(lot["certified_qty"] is not None or lot["uncertified_qty"] is not None for lot in normalized_lots)
    has_any_evidence = has_source_evidence or bool(record_map)

    issues = lot_issues + record_issues + event_issues
    issue_codes = {issue["code"] for issue in issues}
    guaranteed_lots = [row["lot_id"] for row in reports if row["status"] == GUARANTEED]
    review_lots = [row["lot_id"] for row in reports if row["verdict"] == _REVIEW]
    rejected_lots = [row["lot_id"] for row in reports if row["verdict"] == _REJECT]
    pending_lots = review_lots
    signable_lots = guaranteed_lots
    deducted_certified = sum((r["certified_loss_qty"] for r in record_map.values()), Fraction(0))
    attributed_uncertified = sum((r["uncertified_loss_qty"] for r in record_map.values()), Fraction(0))
    total_certified_input = sum((lot["certified_qty"] or Fraction(0) for lot in normalized_lots), Fraction(0))
    initial_certified = as_fraction(
        certified_material_qty if certified_material_qty is not None
        else initial_certified_qty if initial_certified_qty is not None
        else pick(data, ("certified_material_qty", "initial_certified_qty", "certified_stock_qty", "certified_balance_qty")),
        "certified_material_qty",
    )
    if initial_certified is None:
        initial_certified = total_certified_input
    material_ledger = {
        "initial_certified_qty": quantity_json(initial_certified),
        "certified_material_qty": quantity_json(initial_certified),
        "record_count": len(record_map),
        "deducted_certified_qty": quantity_json(deducted_certified),
        "certified_deduction_qty": quantity_json(deducted_certified),
        "certified_loss_qty": quantity_json(deducted_certified),
        "attributed_uncertified_loss_qty": quantity_json(attributed_uncertified),
        "certified_input_qty": quantity_json(total_certified_input),
        "remaining_certified_qty": quantity_json(max(Fraction(0), initial_certified - deducted_certified)),
        "certified_balance_qty": quantity_json(max(Fraction(0), initial_certified - deducted_certified)),
        "idempotent": True,
    }
    counts = {
        "batches": len(reports),
        "guaranteed": sum(r["status"] == GUARANTEED for r in reports),
        "PASS": sum(r["verdict"] == _PASS for r in reports),
        "REVIEW": sum(r["verdict"] == _REVIEW for r in reports),
        "REJECT": sum(r["verdict"] == _REJECT for r in reports),
        "records": len(record_map),
        "duplicate_records": len(duplicate_records),
        "issues": len(issues),
    }
    return {
        "schema": "certification-commitment/v1",
        "enabled": enabled,
        "threshold": fraction_float(global_threshold),
        "required_ratio": fraction_float(global_threshold),
        "threshold_text": percent_text(global_threshold),
        "counts": counts,
        "material_ledger": material_ledger,
        "deducted_certified_qty": material_ledger["deducted_certified_qty"],
        "certified_deduction_qty": material_ledger["certified_deduction_qty"],
        "batches": reports,
        "commitments": reports,
        "certified_batches": reports,
        "workbench": reports,
        "by_lot_id": {row["lot_id"]: row for row in reports},
        "batches_by_id": {row["lot_id"]: row for row in reports},
        "commitment_map": {row["lot_id"]: row for row in reports},
        "guaranteed_lots": guaranteed_lots,
        "review_lots": review_lots,
        "pending_lots": pending_lots,
        "rejected_lots": rejected_lots,
        "blocked_lots": rejected_lots,
        "signable_lots": signable_lots,
        "issuable_lots": guaranteed_lots,
        # 同时给出 items/rows/results 别名，方便不同页面/测试读取。
        "items": reports,
        "rows": reports,
        "results": reports,
        "records": [_record_json(r) for r in record_map.values()],
        "duplicate_records": [_record_json(r) for r in duplicate_records],
        "issues": issues,
        "issue_codes": sorted(issue_codes),
    }


def _apply_sources(raw_lots: Any, sources: list[Any]) -> list[dict[str, Any]]:
    lots = [dict(raw) if isinstance(raw, dict) else raw for raw in as_list(raw_lots)]
    by_index_id: dict[str, dict[str, Any]] = {}
    for raw in lots:
        if isinstance(raw, dict):
            lot_id = canonical(pick(raw, _LOT_KEYS))
            if lot_id:
                by_index_id[lot_id] = raw
    for source in sources:
        if not isinstance(source, dict):
            continue
        lot_id = canonical(pick(source, _LOT_KEYS))
        target = by_index_id.get(lot_id)
        if target is None:
            source_copy = dict(source)
            lots.append(source_copy)
            lot_id = canonical(pick(source_copy, _LOT_KEYS))
            if lot_id:
                by_index_id[lot_id] = source_copy
            continue
        for key in _CERT_QTY_KEYS:
            value = pick(source, (key,))
            if value is not None:
                target.setdefault("certified_qty", value)
        for key in _UNCERT_QTY_KEYS:
            value = pick(source, (key,))
            if value is not None:
                target.setdefault("uncertified_qty", value)
        value = pick(source, _CERT_RATIO_KEYS)
        if value is not None:
            target.setdefault("certified_ratio", value)
        value = pick(source, _THRESHOLD_KEYS)
        if value is not None:
            target.setdefault("required_ratio", value)
    return lots


def _record_json(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record["record_id"],
        "lot_id": record["lot_id"],
        "loss_id": record["loss_id"],
        "certified_loss_qty": quantity_json(record["certified_loss_qty"]),
        "uncertified_loss_qty": quantity_json(record["uncertified_loss_qty"]),
        "digest": record["digest"],
    }


def post_sorting_record(
    state: dict[str, Any] | None,
    record: dict[str, Any],
    lots: Any = None,
    threshold: Any = None,
) -> dict[str, Any]:
    """幂等登记一份分拣记录并返回最新核算。

    ``state`` 可以是 ``None`` 或包含 ``records`` 的字典。相同 record_id + 相同
    内容重试时返回 ``duplicate=True``，不会增加扣减。
    """
    current = dict(state or {})
    records = list(current.get("records", []))
    existing_map, _, _ = normalize_records(records)
    normalized = normalize_record(record)
    existing = existing_map.get(normalized["record_id"])
    duplicate = existing is not None and existing["digest"] == normalized["digest"]
    conflict = existing is not None and existing["digest"] != normalized["digest"]
    if conflict:
        raise CertificationError("RECORD_CONFLICT")
    if not duplicate:
        records.append(_record_json(normalized))
    report = evaluate_certification(
        lots if lots is not None else current.get("lots", []),
        records=records,
        threshold=threshold if threshold is not None else current.get("threshold"),
    )
    target = normalized["lot_id"]
    affected = [target] if target else []
    if target:
        normalized_lots, _ = normalize_lots(lots if lots is not None else current.get("lots", []))
        _, _, children, _ = build_graph(normalized_lots)
        affected.extend(sorted(descendants_of(target, children)))
    report["posting"] = {
        "record": _record_json(normalized),
        "duplicate": duplicate,
        "created": not duplicate,
        "affected_lot_ids": sorted(set(affected)),
    }
    report["affected_lot_ids"] = sorted(set(affected))
    report["records_state"] = records
    return report


# 常见别名，降低不同调用方的命名猜测成本。
calculate_certification = evaluate_certification
calculate_commitment = evaluate_certification
calculate_commitments = evaluate_certification
compute_commitment = evaluate_certification
compute_commitments = evaluate_certification
certification_workbench = evaluate_certification
committed_ratios = evaluate_certification
recalculate = evaluate_certification
add_sorting_record = post_sorting_record
apply_sorting_record = post_sorting_record
record_sorting = post_sorting_record


def committed_ratio(lots: Any, lot_id: Any = None, **kwargs: Any) -> Fraction | None:
    """单批便捷函数，返回精确的最低可承诺占比。

    支持 ``committed_ratio(lot_dict)`` 或 ``committed_ratio(lots, "L1")``。
    """
    target_id = lot_id
    if target_id is not None and not isinstance(target_id, str) and "threshold" not in kwargs and "required_ratio" not in kwargs:
        kwargs["threshold"] = target_id
        target_id = None
    if target_id is None and isinstance(lots, dict):
        target_id = canonical(pick(lots, _LOT_KEYS)) or "LOT"
    report = evaluate_certification(lots, **kwargs)
    if target_id is None:
        item = report["batches"][0]
    else:
        item = next((item for item in report["batches"] if item["lot_id"] == str(target_id)), None)
        if item is None and report["batches"]:
            item = report["batches"][0]
    if item is None:
        return None
    fraction = item.get("committed_ratio_fraction") or item.get("ratio_fraction")
    if fraction and "/" in fraction:
        return Fraction(fraction)
    value = item["committed_ratio_exact"]
    if not value:
        return None
    return Fraction(value) if "/" in value else Fraction(value)
