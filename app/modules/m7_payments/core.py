from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from app.errors import DomainError
from app.modules.m4_allocations.core import allocate
from app.modules.m5_remainders.core import largest_remainder

CENT = Decimal("0.01")

# 指令状态机（单调，不允许倒退）
PENDING = "PENDING"                      # 待付：已占款，未发送支付端
SUBMITTED = "SUBMITTED"                  # 支付端已受理：仍占款，回执未到
PAID = "PAID"                            # 实付成功且未撤回
FAILED = "FAILED"                        # 支付失败：占款释放回可用
VOIDED = "VOIDED"                        # 撤回作废（未实付）：占款释放回可用
RECOVERY_PENDING = "RECOVERY_PENDING"    # 撤回后实付，待追回
RECOVERED = "RECOVERED"                  # 追回款已到账并释放回可用

OCCUPYING_STATES = {PENDING, SUBMITTED}
PAID_STATES = {PAID, RECOVERY_PENDING, RECOVERED}
TERMINAL_STATES = {PAID, FAILED, VOIDED, RECOVERY_PENDING, RECOVERED}

# 允许的状态迁移；SUBMITTED 撤回只置标记不改状态，故不在此表
ALLOWED_TRANSITIONS = {
    PENDING: {SUBMITTED, FAILED, VOIDED},
    SUBMITTED: {PAID, FAILED, VOIDED, RECOVERY_PENDING},
    PAID: {RECOVERY_PENDING},
    RECOVERY_PENDING: {RECOVERED},
    FAILED: set(),
    VOIDED: set(),
    RECOVERED: set(),
}

SCENARIOS = {"ok", "reject", "timeout", "interrupt"}
BANK_ACCEPTED = "ACCEPTED"
BANK_SUCCESS = "SUCCESS"
BANK_FAILED = "FAILED"


def money(value, field: str = "amount") -> Decimal:
    """金额统一量化到分，拒绝非有限值与负值。"""
    try:
        amount = Decimal(str(value).replace(",", ""))
    except Exception as exc:  # InvalidOperation 等
        raise DomainError("INVALID_VALUE", f"{field} 不是合法金额: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise DomainError("INVALID_VALUE", f"{field} 金额非法: {value!r}")
    return amount.quantize(CENT)


def allocate_to_cents(total, items: list[dict]) -> tuple[dict[str, Decimal], Decimal]:
    """沿用 M4 迭代封顶 + M5 最大余额法落到分；返回(分位金额, 未分配余额)。

    输入顺序变化不得改变结果：M4 按权重/封顶裁决，M5 尾差按余额降序、编号升序破平。
    """
    total = money(total, "total")
    norm = []
    for item in items:
        cap = money(item["cap"], "cap").quantize(CENT, rounding=ROUND_DOWN)
        norm.append({"id": str(item["id"]), "weight": Decimal(str(item["weight"])), "cap": cap})
    raw, _left = allocate(total, norm)
    positive = {k: v for k, v in raw.items() if v > 0}
    if not positive:
        return {}, total
    sub_total = sum(positive.values(), Decimal(0))
    shares = {k: (v / sub_total) for k, v in positive.items()}
    rounded = largest_remainder(sub_total, shares)
    # 防御性封顶修正：常态下两位小数封顶不会触发，触发时按尾差顺序把越界分值让给有额度的项
    caps = {x["id"]: x["cap"] for x in norm}
    order = sorted(positive, key=lambda k: (-(positive[k] - rounded[k]), k))
    for key in order:
        while rounded[key] > caps[key]:
            rounded[key] -= CENT
            moved = False
            for candidate in order:
                if candidate != key and rounded[candidate] + CENT <= caps[candidate]:
                    rounded[candidate] += CENT
                    moved = True
                    break
            if not moved:  # 理论不可达：M4 已保证总额不超过封顶之和
                raise DomainError("CAP_LIMIT", "尾差落分后仍无法满足封顶约束")
    allocated = sum(rounded.values(), Decimal(0))
    return rounded, total - allocated


def summarize(instructions: list[dict], budget: Decimal) -> dict:
    """按指令流水聚合资金桶。可用资金完全由预算与指令状态派生，单一事实源、无漂移。"""
    occupied = Decimal(0)
    paid = Decimal(0)
    recovery_pending = Decimal(0)
    recovered = Decimal(0)
    by_state: dict[str, int] = {}
    for ins in instructions:
        amount = Decimal(ins["amount"])
        state = ins["state"]
        by_state[state] = by_state.get(state, 0) + 1
        if state in OCCUPYING_STATES:
            occupied += amount
        if state in PAID_STATES:
            paid += amount
        if state == RECOVERY_PENDING:
            recovery_pending += amount
        if state == RECOVERED:
            recovered += amount
    outstanding = paid - recovered                       # 实际留在体系外的资金
    available = budget - occupied - outstanding          # 可用于新批次占款
    balanced = available >= 0 and available == budget - occupied - paid + recovered
    return {
        "budget": str(budget.quantize(CENT)),
        "available": str(available.quantize(CENT)),
        "occupied": str(occupied.quantize(CENT)),
        "paid": str(paid.quantize(CENT)),
        "recovery_pending": str(recovery_pending.quantize(CENT)),
        "recovered": str(recovered.quantize(CENT)),
        "outstanding": str(outstanding.quantize(CENT)),
        "instruction_count": len(instructions),
        "state_counts": by_state,
        "balanced": balanced,
        "invariant": "budget = available + occupied + paid - recovered",
    }


def summarize_batches(instructions: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for ins in instructions:
        bucket = grouped.setdefault(ins["batch_id"], {
            "batch_id": ins["batch_id"], "occupied": Decimal(0), "paid": Decimal(0),
            "recovery_pending": Decimal(0), "recovered": Decimal(0),
            "voided": Decimal(0), "failed": Decimal(0), "count": 0,
        })
        amount = Decimal(ins["amount"])
        bucket["count"] += 1
        state = ins["state"]
        if state in OCCUPYING_STATES:
            bucket["occupied"] += amount
        if state in PAID_STATES:
            bucket["paid"] += amount
        if state == RECOVERY_PENDING:
            bucket["recovery_pending"] += amount
        if state == RECOVERED:
            bucket["recovered"] += amount
        if state == VOIDED:
            bucket["voided"] += amount
        if state == FAILED:
            bucket["failed"] += amount
    out = []
    for bucket in grouped.values():
        out.append({k: (str(v.quantize(CENT)) if isinstance(v, Decimal) else v) for k, v in bucket.items()})
    return sorted(out, key=lambda x: x["batch_id"])
