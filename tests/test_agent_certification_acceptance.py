import json

from app.main import create_app


def test_engine_scenarios():
    from app.certification.engine import evaluate_certification, post_sorting_record

    lots = [{
        "lot_id": "B0", "qty_in": 100, "qty_out": 92, "loss_qty": 8,
        "certified_qty": 92, "required_ratio": 0.92,
    }]
    report = evaluate_certification(lots)
    batch = report["batches"][0]
    # 平均投入占比正好 92%，但未归属损耗最坏全部是认证原料：84/92 < 92%。
    assert batch["average_ratio"] == 0.92
    assert batch["committed_certified_qty"] == 84
    assert batch["verdict"] == "REVIEW"

    state = {"lots": lots, "threshold": 0.92}
    updated = post_sorting_record(state, {
        "record_id": "S1", "lot_id": "B0", "uncertified_loss_qty": 8,
    })
    assert updated["batches"][0]["committed_ratio"] == 1.0
    assert updated["batches"][0]["verdict"] == "PASS"
    retry_state = {"records": updated["records_state"], "lots": lots, "threshold": 0.92}
    retry = post_sorting_record(retry_state, {
        "record_id": "S1", "lot_id": "B0", "uncertified_loss_qty": 8,
    })
    assert retry["posting"]["duplicate"] is True
    assert retry["counts"]["records"] == 1


def test_split_remerge_shared_loss_once():
    from app.certification.engine import evaluate_certification

    lots = [
        {"lot_id": "R", "qty_in": 100, "qty_out": 90, "loss_qty": 10, "certified_qty": 90},
        {"lot_id": "A", "parent_lot": "R", "qty_in": 45, "qty_out": 45, "loss_qty": 0},
        {"lot_id": "B", "parent_lot": "R", "qty_in": 45, "qty_out": 45, "loss_qty": 0},
        {"lot_id": "M", "parent_lot": "A:45;B:45", "qty_in": 90, "qty_out": 90,
         "loss_qty": 0, "required_ratio": 0.9},
    ]
    by_id = {x["lot_id"]: x for x in evaluate_certification(lots)["batches"]}
    batch = by_id["M"]
    assert by_id["A"]["committed_certified_qty"] == 40
    assert batch["committed_certified_qty"] == 80
    assert len(batch["counted_loss_ids"]) == 1
    assert batch["verdict"] == "REVIEW"


def test_http_evaluate_and_idempotent_sorting_record(tmp_path):
    app = create_app(str(tmp_path / "cert.sqlite3"))
    client = app.test_client()
    payload = {
        "threshold": 0.9,
        "lots": [{
            "lot_id": "B1", "qty_in": 100, "qty_out": 90, "loss_qty": 10,
            "certified_qty": 90,
        }],
    }
    response = client.post("/api/certification/evaluate", json=payload)
    assert response.status_code == 200
    assert response.get_json()["batches"][0]["verdict"] == "REVIEW"

    record = {
        "threshold": 0.9,
        "lots": payload["lots"],
        "record_id": "REC-1",
        "lot_id": "B1",
        "uncertified_loss_qty": 10,
    }
    first = client.post("/api/certification/sorting-records", json=record)
    assert first.status_code == 200
    body = first.get_json()
    assert body["posting"]["outcomes"][0]["created"] is True
    assert body["batches"][0]["verdict"] == "PASS"

    second = client.post("/api/certification/sorting-records", json=record)
    assert second.status_code == 200
    body2 = second.get_json()
    assert body2["posting"]["outcomes"][0]["duplicate"] is True
    assert body2["counts"]["records"] == 1
    workbench = client.get("/api/certification/workbench")
    assert workbench.status_code == 200


def test_run_sorting_record_recalculates(tmp_path):
    app = create_app(str(tmp_path / "run.sqlite3"))
    client = app.test_client()
    created = client.post("/api/runs", json={"profile": "clean"}).get_json()
    run_id = created["run_id"]
    response = client.post(f"/api/runs/{run_id}/sorting-records", json={
        "record_id": "LATE-1",
        "lot_id": "L4",
        "uncertified_loss_qty": 10,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert "certification" in body
    assert "L4" in body["affected_lot_ids"]
    again = client.post(f"/api/runs/{run_id}/sorting-records", json={
        "record_id": "LATE-1",
        "lot_id": "L4",
        "uncertified_loss_qty": 10,
    })
    assert again.status_code == 200
    assert again.get_json()["posting"]["outcomes"][0]["duplicate"] is True
