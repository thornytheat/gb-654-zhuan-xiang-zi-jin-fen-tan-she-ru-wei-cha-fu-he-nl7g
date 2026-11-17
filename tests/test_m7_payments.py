"""M7 本地模拟支付与撤回对账 —— 核心领域规则测试（不接真实银行，不动 M2 资格）。"""
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m7_payments.core import (
    allocate_to_cents, money, summarize,
)
from app.modules.m7_payments.store import PaymentStore

ITEMS = [
    {"id": "A", "weight": "5", "cap": "300.00"},
    {"id": "B", "weight": "3", "cap": "500.00"},
    {"id": "C", "weight": "2", "cap": "500.00"},
]


def _store(tmp_path) -> PaymentStore:
    return PaymentStore(str(tmp_path / "m7.db"))


def _make_pool_and_batch(tmp_path, budget="1000.00", total="1000.00", items=None,
                         pool_id="p1", batch_id="b1"):
    store = _store(tmp_path)
    store.create_pool(pool_id, budget)
    rounded, left = allocate_to_cents(total, items or ITEMS)
    instructions = [{"instruction_id": f"{batch_id}:{k}", "project_id": k, "amount": str(v)}
                    for k, v in sorted(rounded.items())]
    store.create_batch(pool_id, batch_id, "digest-" + batch_id,
                       str(sum(rounded.values(), Decimal(0))), str(left),
                       instructions,
                       lambda ins, b: Decimal(b) - sum((Decimal(i["amount"]) for i in ins
                                                        if i["state"] in ("PENDING", "SUBMITTED")), Decimal(0))
                       - sum((Decimal(i["amount"]) for i in ins
                              if i["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED")), Decimal(0))
                       + sum((Decimal(i["amount"]) for i in ins if i["state"] == "RECOVERED"), Decimal(0)))
    return store, instructions


# ---------- 封顶与尾差沿用现有规则 ----------
def test_allocation_reuses_cap_and_remainder_rules(tmp_path):
    rounded, left = allocate_to_cents("1000.00", ITEMS)
    assert rounded["A"] == Decimal("300.00")          # M4 迭代封顶
    assert sum(rounded.values(), Decimal(0)) + left == Decimal("1000.00")
    assert all(v.as_tuple().exponent >= -2 for v in rounded.values())  # 落到分


def test_allocation_order_independent(tmp_path):
    r1, _ = allocate_to_cents("1000.00", ITEMS)
    r2, _ = allocate_to_cents("1000.00", list(reversed(ITEMS)))
    assert r1 == r2


def test_remainder_cent_distribution_conserves(tmp_path):
    # 33.34/33.33/33.33 类尾差：最大余额法，分位合计严格等于总额
    items = [{"id": "A", "weight": "1", "cap": "999"},
             {"id": "B", "weight": "1", "cap": "999"},
             {"id": "C", "weight": "1", "cap": "999"}]
    rounded, left = allocate_to_cents("100.00", items)
    assert sum(rounded.values(), Decimal(0)) == Decimal("100.00")
    assert left == 0
    assert sorted(rounded.values()) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]


def test_cap_leaves_unallocated(tmp_path):
    rounded, left = allocate_to_cents("10.00", [{"id": "A", "weight": 1, "cap": "3.00"}])
    assert rounded == {"A": Decimal("3.00")}
    assert left == Decimal("7.00")


def test_money_validation():
    assert money("1,200.50") == Decimal("1200.50")
    with pytest.raises(DomainError):
        money("N/A")
    with pytest.raises(DomainError):
        money("-1.00")


# ---------- 撤回：作废待付，但已实付不抹除，转待追回 ----------
def test_withdraw_pending_voids_and_releases_occupation(tmp_path):
    store, ins = _make_pool_and_batch(tmp_path)
    result = store.withdraw_batch("b1")
    assert {a["action"] for a in result["actions"]} == {"VOIDED"}
    rows = store.list_instructions(batch_id="b1")
    assert all(r["state"] == "VOIDED" for r in rows)
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert pos["occupied"] == "0.00"
    assert pos["available"] == "1000.00"
    assert pos["balanced"] is True


def test_withdraw_after_payment_keeps_paid_and_marks_recovery(tmp_path):
    store, ins = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    store.post_receipt(iid, "SUCCESS")
    assert store.list_instructions(batch_id="b1")[0]["state"] == "PAID"
    # 撤回 A：实付如实保留转待追回；B、C 未撤回仍占款，不能直接撤掉扣款
    store.withdraw_batch("b1", ["b1:A"])
    states = {r["instruction_id"]: r["state"] for r in store.list_instructions(batch_id="b1")}
    assert states[iid] == "RECOVERY_PENDING"
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert pos["paid"] == "300.00"                # 实付不抹除
    assert pos["recovery_pending"] == "300.00"
    assert pos["occupied"] == "700.00"            # B、C 仍是 PENDING 占用
    assert pos["available"] == "0.00"


def test_late_success_receipt_after_withdraw_becomes_recovery(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:B"
    store.send_payment(iid, "req-B")               # SUBMITTED
    store.withdraw_batch("b1")                     # 标记撤回，状态仍 SUBMITTED
    row = next(r for r in store.list_instructions(batch_id="b1") if r["instruction_id"] == iid)
    assert row["state"] == "SUBMITTED" and row["withdraw_flag"] == 1
    store.post_receipt(iid, "SUCCESS")             # 迟到成功回执：如实入账转待追回
    row = next(r for r in store.list_instructions(batch_id="b1") if r["instruction_id"] == iid)
    assert row["state"] == "RECOVERY_PENDING"
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert pos["paid"] == "420.00"
    assert pos["recovery_pending"] == "420.00"


def test_unrecovered_funds_not_released_to_other_batches(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path, budget="1000.00")
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    store.post_receipt(iid, "SUCCESS")
    store.withdraw_batch("b1", [iid])
    # 待追回 300 未到账：新批次只能用剩余 700，超预算必须拒绝
    rounded, _ = allocate_to_cents("701.00", [{"id": "X", "weight": 1, "cap": "701.00"}])
    ins2 = [{"instruction_id": "b2:X", "project_id": "X", "amount": str(v)} for v in rounded.values()]
    available = lambda rows, b: (Decimal(b)
        - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PENDING", "SUBMITTED")), Decimal(0))
        - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED")), Decimal(0))
        + sum((Decimal(i["amount"]) for i in rows if i["state"] == "RECOVERED"), Decimal(0)))
    with pytest.raises(DomainError) as exc:
        store.create_batch("p1", "b2", "digest-b2", "701.00", "0.00", ins2, available)
    assert exc.value.code == "BUDGET_EXCEEDED"
    # 全额追回到账后才释放
    store.register_recovery(iid, "300.00")
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert pos["recovery_pending"] == "0.00"
    assert pos["recovered"] == "300.00"
    assert pos["available"] == "300.00"             # 700 仍被 b1 其余指令占用


def test_partial_recovery_rejected(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    store.post_receipt(iid, "SUCCESS")
    store.withdraw_batch("b1", [iid])
    with pytest.raises(DomainError) as exc:
        store.register_recovery(iid, "299.99")
    assert exc.value.code == "RECOVERY_AMOUNT_MISMATCH"
    # 全额追回后进入终态，再次登记（任何金额）都被拒绝
    store.register_recovery(iid, "300.00")
    with pytest.raises(DomainError) as exc:
        store.register_recovery(iid, "300.00")
    assert exc.value.code == "NOT_RECOVERABLE"


# ---------- 超时先查受理、中断恢复、不重复扣款 ----------
def test_timeout_then_retry_reuses_acceptance_no_double_charge(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.set_scenario(iid, "timeout")
    with pytest.raises(DomainError) as exc:
        store.send_payment(iid, "req-A")
    assert exc.value.code == "BANK_TIMEOUT"
    status = store.get_bank_status(iid)
    assert status["accepted"] is True and status["bank_state"] == "ACCEPTED"
    # 重试：先查受理，已受理直接复用，不产生第二个银行请求
    result = store.send_payment(iid, "req-A")
    assert result["reused_acceptance"] is True
    assert result["bank_state"] == "ACCEPTED"
    requests = [e for e in store.list_events(iid) if e["event"] == "PAYMENT_SENT"]
    assert len(requests) == 1
    assert next(r for r in store.list_instructions() if r["instruction_id"] == iid)["state"] == "SUBMITTED"


def test_interrupted_send_recovers_without_double_charge(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:B"
    store.set_scenario(iid, "interrupt")
    with pytest.raises(DomainError) as exc:
        store.send_payment(iid, "req-B")
    assert exc.value.code == "BANK_INTERRUPTED"
    # 中断后恢复：受理记录已落库，重试复用；场景已自动复位
    result = store.send_payment(iid, "req-B")
    assert result["reused_acceptance"] is True
    sent = [e for e in store.list_events(iid) if e["event"] == "PAYMENT_SENT"]
    assert len(sent) == 1
    store.post_receipt(iid, "SUCCESS")
    assert next(r for r in store.list_instructions() if r["instruction_id"] == iid)["state"] == "PAID"


def test_reject_scenario_fails_and_releases_occupation(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:C"
    store.set_scenario(iid, "reject")
    result = store.send_payment(iid, "req-C")
    assert result["bank_state"] == "FAILED"
    assert next(r for r in store.list_instructions() if r["instruction_id"] == iid)["state"] == "FAILED"
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    # C 的 280 释放回可用
    assert pos["available"] == "280.00"
    assert pos["occupied"] == "720.00"


def test_retry_after_unknown_state_queries_first(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    again = store.send_payment(iid, "req-A")
    assert again["reused_acceptance"] is True and again["attempts"] == 2


# ---------- 乱序回执不倒退、幂等 ----------
def test_out_of_order_receipts_do_not_regress(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    store.post_receipt(iid, "SUCCESS")
    # 迟到的失败回执不得把 PAID 倒退
    with pytest.raises(DomainError) as exc:
        store.post_receipt(iid, "FAILED")
    assert exc.value.code == "RECEIPT_CONFLICT"
    # 重复成功回执幂等
    again = store.post_receipt(iid, "SUCCESS")
    assert again["idempotent"] is True
    assert next(r for r in store.list_instructions() if r["instruction_id"] == iid)["state"] == "PAID"


def test_duplicate_failed_receipt_idempotent(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.set_scenario(iid, "reject")
    store.send_payment(iid, "req-A")
    r1 = store.post_receipt(iid, "FAILED")
    r2 = store.post_receipt(iid, "FAILED")
    assert r1["state"] == r2["state"] == "FAILED"
    assert r2["idempotent"] is True


def test_receipt_without_acceptance_rejected(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    with pytest.raises(DomainError) as exc:
        store.post_receipt("b1:A", "SUCCESS")
    assert exc.value.code == "NO_ACCEPTANCE"


# ---------- 状态机不允许倒退 ----------
def test_state_machine_is_monotonic(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    iid = "b1:A"
    store.send_payment(iid, "req-A")
    store.post_receipt(iid, "SUCCESS")
    # 已终态的指令不可再发送支付
    with pytest.raises(DomainError) as exc:
        store.send_payment(iid, "req-A2")
    assert exc.value.code == "INVALID_TRANSITION"


# ---------- 跨批次并发占款不超预算 ----------
def test_cross_batch_occupation_within_budget(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path, budget="1000.00")  # 占 1000
    rounded, _ = allocate_to_cents("1.00", [{"id": "X", "weight": 1, "cap": "1.00"}])
    ins2 = [{"instruction_id": "b2:X", "project_id": "X", "amount": str(rounded["X"])}]
    available = lambda rows, b: (Decimal(b)
        - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PENDING", "SUBMITTED")), Decimal(0))
        - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED")), Decimal(0))
        + sum((Decimal(i["amount"]) for i in rows if i["state"] == "RECOVERED"), Decimal(0)))
    with pytest.raises(DomainError) as exc:
        store.create_batch("p1", "b2", "digest-b2", "1.00", "0.00", ins2, available)
    assert exc.value.code == "BUDGET_EXCEEDED"


def test_concurrent_batch_creation_serialized(tmp_path):
    """多线程并发建批次：BEGIN IMMEDIATE 串行化，合计占款绝不超过预算。"""
    import threading
    store = _store(tmp_path)
    store.create_pool("p1", "1000.00")
    errors = []

    def worker(batch_id, amount):
        try:
            rounded, _ = allocate_to_cents(amount, [{"id": "X", "weight": 1, "cap": amount}])
            ins = [{"instruction_id": f"{batch_id}:X", "project_id": "X", "amount": str(rounded["X"])}]
            available = lambda rows, b: (Decimal(b)
                - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PENDING", "SUBMITTED")), Decimal(0))
                - sum((Decimal(i["amount"]) for i in rows if i["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED")), Decimal(0))
                + sum((Decimal(i["amount"]) for i in rows if i["state"] == "RECOVERED"), Decimal(0)))
            store.create_batch("p1", batch_id, f"digest-{batch_id}", amount, "0.00", ins, available)
        except DomainError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(f"b{i}", "600.00")) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert Decimal(pos["occupied"]) <= Decimal("1000.00")
    assert errors.count("BUDGET_EXCEEDED") >= 2  # 最多放 1 个 600 批次，其余被拒
    assert pos["balanced"] is True


# ---------- 重启恢复（持久化） ----------
def test_state_survives_store_restart(tmp_path):
    path = str(tmp_path / "m7.db")
    store = PaymentStore(path)
    store.create_pool("p1", "1000.00")
    rounded, _ = allocate_to_cents("100.00", [{"id": "A", "weight": 1, "cap": "100.00"}])
    ins = [{"instruction_id": "b1:A", "project_id": "A", "amount": "100.00"}]
    available = lambda rows, b: Decimal(b) - sum((Decimal(i["amount"]) for i in rows), Decimal(0))
    store.create_batch("p1", "b1", "d1", "100.00", "0.00", ins, available)
    store.set_scenario("b1:A", "timeout")
    with pytest.raises(DomainError):
        store.send_payment("b1:A", "req-A")
    # 模拟进程重启：新实例打开同一数据库，受理状态仍在，重试不重复扣款
    store2 = PaymentStore(path)
    status = store2.get_bank_status("b1:A")
    assert status["accepted"] is True
    result = store2.send_payment("b1:A", "req-A")
    assert result["reused_acceptance"] is True


# ---------- 幂等建批次 ----------
def test_duplicate_batch_digest_rejected(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path)
    rounded, _ = allocate_to_cents("1000.00", ITEMS)
    ins = [{"instruction_id": "bX:A", "project_id": k, "amount": str(v)} for k, v in rounded.items()]
    available = lambda rows, b: Decimal("1000.00")
    with pytest.raises(DomainError) as exc:
        store.create_batch("p1", "bX", "digest-b1", "1000.00", "0.00", ins, available)
    assert exc.value.code == "DUPLICATE_BATCH"


# ---------- 全链路守恒 ----------
def test_full_lifecycle_invariant_holds(tmp_path):
    store, _ = _make_pool_and_batch(tmp_path, budget="1000.00")
    # A 走成功支付后撤回 -> 待追回 -> 追回（只撤回 A，B/C 保持待付继续各自链路）
    store.send_payment("b1:A", "req-A")
    store.post_receipt("b1:A", "SUCCESS")
    store.withdraw_batch("b1", ["b1:A"])
    store.register_recovery("b1:A", "300.00")
    # B 超时 -> 重试恢复 -> 成功
    store.set_scenario("b1:B", "timeout")
    with pytest.raises(DomainError):
        store.send_payment("b1:B", "req-B")
    store.send_payment("b1:B", "req-B")
    store.post_receipt("b1:B", "SUCCESS")
    # C 被拒 -> 失败释放
    store.set_scenario("b1:C", "reject")
    store.send_payment("b1:C", "req-C")
    pos = summarize(store.list_instructions(pool_id="p1"), Decimal("1000.00"))
    assert pos["balanced"] is True
    assert pos["paid"] == "720.00"                  # A 300 + B 420
    assert pos["recovered"] == "300.00"
    assert pos["occupied"] == "0.00"
    # budget = available + occupied + paid - recovered
    assert Decimal(pos["available"]) == Decimal("1000.00") - Decimal(pos["occupied"]) \
        - Decimal(pos["paid"]) + Decimal(pos["recovered"])
    events = store.list_events()
    assert any(e["event"] == "BATCH_WITHDRAWN" for e in events)
    assert any(e["event"] == "RECOVERY_REGISTERED" for e in events)
