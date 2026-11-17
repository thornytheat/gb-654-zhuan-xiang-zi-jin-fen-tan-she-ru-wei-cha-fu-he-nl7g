from __future__ import annotations

from decimal import Decimal

from app.manifest import canonical_digest
from .core import CENT, allocate_to_cents, money, summarize, summarize_batches
from .store import PaymentStore

SAMPLE = {
    "pool_id": "pool-demo",
    "budget": "1000.00",
    "batch_id": "batch-demo-01",
    "total": "1000.00",
    "items": [
        {"id": "A", "weight": "5", "cap": "300.00"},
        {"id": "B", "weight": "3", "cap": "500.00"},
        {"id": "C", "weight": "2", "cap": "500.00"},
    ],
}


def _store(request) -> PaymentStore:
    return request.app.state.payment_store


def _available_amount(instructions: list[dict], budget: str) -> Decimal:
    """实时可用 = 预算 - 占用 - 在外实付（含待追回）+ 已追回释放。"""
    budget = money(budget)
    occupied = sum((Decimal(i["amount"]) for i in instructions
                    if i["state"] in ("PENDING", "SUBMITTED")), Decimal(0))
    paid = sum((Decimal(i["amount"]) for i in instructions
                if i["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED")), Decimal(0))
    recovered = sum((Decimal(i["amount"]) for i in instructions
                     if i["state"] == "RECOVERED"), Decimal(0))
    return budget - occupied - paid + recovered


def create_pool(request, payload: dict) -> dict:
    pool_id = str(payload["pool_id"])
    budget = money(payload.get("budget", "0"), "budget")
    return _store(request).create_pool(pool_id, str(budget))


def create_batch(request, payload: dict) -> dict:
    pool_id = str(payload["pool_id"])
    total = money(payload.get("total", "0"), "total")
    items = payload.get("items") or []
    if not items:
        from app.errors import DomainError
        raise DomainError("EMPTY_BATCH", "批次至少包含一个分摊项目")
    rounded, unallocated = allocate_to_cents(total, items)
    digest_src = [{"project_id": k, "amount": str(v)} for k, v in sorted(rounded.items())]
    digest = canonical_digest({"pool_id": pool_id, "total": str(total), "rows": digest_src})
    batch_id = str(payload.get("batch_id") or ("batch-" + digest[:12]))
    instructions = [
        {
            "instruction_id": f"{batch_id}:{project_id}",
            "project_id": project_id,
            "amount": str(amount),
        }
        for project_id, amount in sorted(rounded.items())
    ]
    created = _store(request).create_batch(
        pool_id, batch_id, digest, str(sum(rounded.values(), Decimal(0))),
        str(unallocated), instructions, _available_amount)
    return {**created, "instructions": instructions,
            "allocation": {k: str(v) for k, v in rounded.items()}}


def _get_pool_id(request, pool_id: str | None) -> str:
    if pool_id:
        return pool_id
    pools = _store(request).list_pools()
    if not pools:
        from app.errors import DomainError
        raise DomainError("POOL_NOT_FOUND", "尚无资金池，请先创建")
    return pools[0]["pool_id"]


def positions(request, pool_id: str | None = None) -> dict:
    store = _store(request)
    pool_id = _get_pool_id(request, pool_id)
    pool = store.get_pool(pool_id)
    instructions = store.list_instructions(pool_id=pool_id)
    summary = summarize(instructions, money(pool["budget"], "budget"))
    return {"pool_id": pool_id, **summary, "batches": summarize_batches(instructions)}


def send_payment(request, instruction_id: str) -> dict:
    store = _store(request)
    request_id = "req-" + canonical_digest({"instruction_id": instruction_id})[:16]
    return store.send_payment(instruction_id, request_id, _available_amount)


def bank_status(request, instruction_id: str) -> dict:
    return _store(request).get_bank_status(instruction_id)


def post_receipt(request, instruction_id: str, outcome: str) -> dict:
    return _store(request).post_receipt(instruction_id, outcome)


def withdraw(request, payload: dict) -> dict:
    batch_id = str(payload["batch_id"])
    ids = payload.get("instruction_ids")
    if ids is not None:
        ids = [str(x) for x in ids]
    return _store(request).withdraw_batch(batch_id, ids)


def register_recovery(request, instruction_id: str, amount: str) -> dict:
    return _store(request).register_recovery(instruction_id, str(money(amount, "amount")))


def batch_detail(request, batch_id: str) -> dict:
    store = _store(request)
    batch = store.get_batch(batch_id)
    return {**batch, "instructions": store.list_instructions(batch_id=batch_id)}


def list_batches(request, pool_id: str | None = None) -> dict:
    return {"batches": _store(request).list_batches(pool_id)}


def list_pools(request) -> dict:
    return {"pools": _store(request).list_pools()}


def list_events(request, ref_id: str | None = None) -> dict:
    return {"events": _store(request).list_events(ref_id)}


def set_scenario(request, payload: dict) -> dict:
    key = str(payload.get("key", "default"))
    return _store(request).set_scenario(key, str(payload["scenario"]))


def overview(request) -> dict:
    """页面总览：无数据时返回空骨架，不自动写库。"""
    store = _store(request)
    pools = store.list_pools()
    if not pools:
        return {"status": "EMPTY", "pools": [], "positions": None, "batches": []}
    pool_id = pools[0]["pool_id"]
    return {"status": "READY", "pools": pools,
            "positions": positions(request, pool_id),
            "batches": store.list_batches(pool_id)}
