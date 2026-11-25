from app.domain.helpers import D


def decide_rows(items):
    for item in items:
        if item.get("problem"):
            item["verdict"] = "REJECT"
            item["reason"] = item["problem"]
        elif item["loss_qty"] * 2 > item["qty_in"]:
            item["verdict"] = "REJECT"
            item["reason"] = "LOSS_OVER_HALF"
        elif item["affected"]:
            item["verdict"] = "REVIEW"
            item["reason"] = "RECALL_CLOSURE"
        elif item["qty_in"] != item["qty_out"] + item["loss_qty"]:
            item["verdict"] = "REJECT"
            item["reason"] = "MASS_MISMATCH"
        elif item["loss_qty"] > item["qty_in"] // 5:
            item["verdict"] = "REVIEW"
            item["reason"] = "LOSS_OVER_20"
        else:
            item["verdict"] = "PASS"
            item["reason"] = "CONSERVED"
        cert = item.get("certification") if isinstance(item.get("certification"), dict) else {}
        cert_verdict = item.get("certification_verdict") or cert.get("verdict")
        if cert_verdict:
            severity = {"PASS": 0, "REVIEW": 1, "REJECT": 2}
            if severity.get(cert_verdict, 1) > severity.get(item["verdict"], 1):
                item["verdict"] = cert_verdict
                item["reason"] = cert.get("reason") or item.get("certification_status") or "CERT_REVIEW"
        item.setdefault("verdict", "PASS")
        item.setdefault("reason", "OK")
    return items
