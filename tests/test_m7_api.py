"""M7 HTTP 接口集成测试：完整撤回/支付/对账链路，不接真实银行。"""


def _create_pool(client, pool_id="p1", budget="1000.00"):
    return client.post("/api/payments/pools", json={"pool_id": pool_id, "budget": budget})


def _create_batch(client, batch_id="b1", pool_id="p1", total="1000.00"):
    items = [
        {"id": "A", "weight": "5", "cap": "300.00"},
        {"id": "B", "weight": "3", "cap": "500.00"},
        {"id": "C", "weight": "2", "cap": "500.00"},
    ]
    return client.post("/api/payments/batches",
                       json={"pool_id": pool_id, "batch_id": batch_id, "total": total, "items": items})


def test_payments_page_ok(client):
    response = client.get("/payments")
    assert response.status_code == 200
    assert "panel" in response.text


def test_full_workflow_via_api(client):
    assert _create_pool(client).status_code == 200
    batch = _create_batch(client).json()
    assert batch["allocation"]["A"] == "300.00"
    assert sum(float(v) for v in batch["allocation"].values()) == 1000.0
    iid_a, iid_b, iid_c = "b1:A", "b1:B", "b1:C"

    # A 正常支付成功
    sent = client.post(f"/api/payments/instructions/{iid_a}/send").json()
    assert sent["bank_state"] == "ACCEPTED" and sent["instruction_state"] == "SUBMITTED"
    assert client.post(f"/api/payments/instructions/{iid_a}/receipt",
                       json={"outcome": "SUCCESS"}).json()["state"] == "PAID"

    # 撤回整批：A 已实付转待追回；B、C 待付作废
    withdrawn = client.post("/api/payments/withdraw", json={"batch_id": "b1"}).json()
    actions = {a["instruction_id"]: a["action"] for a in withdrawn["actions"]}
    assert actions[iid_a] == "RECOVERY_PENDING"
    assert actions[iid_b] == "VOIDED" and actions[iid_c] == "VOIDED"

    # B 的迟到成功回执：但 B 已作废且无受理记录 -> 拒绝（不会凭空入账）
    resp = client.post(f"/api/payments/instructions/{iid_b}/receipt", json={"outcome": "SUCCESS"})
    assert resp.status_code == 422 and resp.json()["error"] == "NO_ACCEPTANCE"

    # 待追回不释放；全额追回后才释放
    positions = client.get("/api/payments/positions").json()
    assert positions["paid"] == "300.00"
    assert positions["recovery_pending"] == "300.00"
    assert positions["available"] == "700.00"
    assert positions["balanced"] is True

    rec = client.post(f"/api/payments/instructions/{iid_a}/recover", json={"amount": "300.00"})
    assert rec.json()["state"] == "RECOVERED"
    positions = client.get("/api/payments/positions").json()
    assert positions["recovery_pending"] == "0.00"
    assert positions["recovered"] == "300.00"
    assert positions["available"] == "1000.00"


def test_timeout_retry_and_interrupt_recovery_via_api(client):
    _create_pool(client)
    _create_batch(client)
    # 超时：首次 422 信号，但受理已落库；重试复用不重复扣款
    client.post("/api/payments/scenarios", json={"key": "b1:B", "scenario": "timeout"})
    first = client.post("/api/payments/instructions/b1:B/send")
    assert first.status_code == 422 and first.json()["error"] == "BANK_TIMEOUT"
    status = client.get("/api/payments/instructions/b1:B/bank-status").json()
    assert status["accepted"] is True
    retry = client.post("/api/payments/instructions/b1:B/send").json()
    assert retry["reused_acceptance"] is True
    # 中断：受理落库后自动复位，再试走正常链路
    client.post("/api/payments/scenarios", json={"key": "b1:C", "scenario": "interrupt"})
    interrupted = client.post("/api/payments/instructions/b1:C/send")
    assert interrupted.status_code == 422 and interrupted.json()["error"] == "BANK_INTERRUPTED"
    recovered = client.post("/api/payments/instructions/b1:C/send").json()
    assert recovered["reused_acceptance"] is True
    events = client.get("/api/payments/events", params={"ref_id": "b1:B"}).json()["events"]
    assert sum(1 for e in events if e["event"] == "PAYMENT_SENT") == 1


def test_out_of_order_receipts_not_regress_via_api(client):
    _create_pool(client)
    _create_batch(client)
    client.post("/api/payments/instructions/b1:A/send")
    client.post("/api/payments/instructions/b1:A/receipt", json={"outcome": "SUCCESS"})
    conflict = client.post("/api/payments/instructions/b1:A/receipt", json={"outcome": "FAILED"})
    assert conflict.status_code == 422 and conflict.json()["error"] == "RECEIPT_CONFLICT"
    dup = client.post("/api/payments/instructions/b1:A/receipt", json={"outcome": "SUCCESS"}).json()
    assert dup["idempotent"] is True and dup["state"] == "PAID"


def test_cross_batch_budget_enforced_via_api(client):
    _create_pool(client, budget="1000.00")
    _create_batch(client, batch_id="b1")
    # 预算已被 b1 占满，b2 哪怕 1 分也不允许
    resp = client.post("/api/payments/batches", json={
        "pool_id": "p1", "batch_id": "b2", "total": "1.00",
        "items": [{"id": "X", "weight": 1, "cap": "1.00"}]})
    assert resp.status_code == 422 and resp.json()["error"] == "BUDGET_EXCEEDED"


def test_batch_idempotent_by_digest_via_api(client):
    _create_pool(client)
    first = _create_batch(client, batch_id="b1").json()
    second = client.post("/api/payments/batches", json={
        "pool_id": "p1", "batch_id": "b1-copy", "total": "1000.00",
        "items": [
            {"id": "A", "weight": "5", "cap": "300.00"},
            {"id": "B", "weight": "3", "cap": "500.00"},
            {"id": "C", "weight": "2", "cap": "500.00"},
        ]})
    assert second.status_code == 422 and second.json()["error"] == "DUPLICATE_BATCH"
    assert first["digest"]


def test_withdraw_submitted_then_late_success_to_recovery_via_api(client):
    _create_pool(client)
    _create_batch(client)
    client.post("/api/payments/instructions/b1:A/send")  # SUBMITTED
    client.post("/api/payments/withdraw", json={"batch_id": "b1", "instruction_ids": ["b1:A"]})
    receipt = client.post("/api/payments/instructions/b1:A/receipt",
                          json={"outcome": "SUCCESS"}).json()
    assert receipt["state"] == "RECOVERY_PENDING"
    positions = client.get("/api/payments/positions").json()
    assert positions["paid"] == "300.00" and positions["recovery_pending"] == "300.00"


def test_reject_releases_occupation_via_api(client):
    _create_pool(client)
    _create_batch(client)
    client.post("/api/payments/scenarios", json={"key": "b1:C", "scenario": "reject"})
    out = client.post("/api/payments/instructions/b1:C/send").json()
    assert out["instruction_state"] == "FAILED"
    positions = client.get("/api/payments/positions").json()
    assert positions["occupied"] == "720.00" and positions["available"] == "280.00"
