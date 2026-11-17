from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.errors import DomainError
from .core import ALLOWED_TRANSITIONS, BANK_ACCEPTED, BANK_FAILED, BANK_SUCCESS, SCENARIOS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaymentStore:
    """M7 本地台账与模拟支付端。同库持久化，进程中断/重启后状态可恢复，不接真实银行。"""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS m7_pools (
                    pool_id TEXT PRIMARY KEY,
                    budget TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m7_batches (
                    batch_id TEXT PRIMARY KEY,
                    pool_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    total TEXT NOT NULL,
                    unallocated TEXT NOT NULL,
                    withdrawn INTEGER NOT NULL DEFAULT 0,
                    withdrawn_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(pool_id, digest)
                );
                CREATE TABLE IF NOT EXISTS m7_instructions (
                    instruction_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    state TEXT NOT NULL,
                    withdraw_flag INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m7_bank_requests (
                    request_id TEXT PRIMARY KEY,
                    instruction_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m7_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    ref_type TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m7_scenarios (
                    key TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    # ---------- 基础工具 ----------
    def write(self, fn):
        """串行化写事务；数据库锁冲突时有限重试，保证跨批次并发不超预算。"""
        last = None
        for _ in range(5):
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                result = fn(db)
                db.commit()
                return result
            except sqlite3.OperationalError as exc:  # database is locked
                db.rollback()
                last = exc
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        raise last  # pragma: no cover - 高锁竞争才会到达

    def read(self, fn):
        db = self._connect()
        try:
            return fn(db)
        finally:
            db.close()

    def event(self, db, ref_type: str, ref_id: str, event: str, detail: dict) -> None:
        db.execute(
            "INSERT INTO m7_events(ts, ref_type, ref_id, event, detail) VALUES (?,?,?,?,?)",
            (utc_now(), ref_type, ref_id, event, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )

    @staticmethod
    def _guard_transition(old: str, new: str) -> None:
        if new not in ALLOWED_TRANSITIONS.get(old, set()):
            raise DomainError("INVALID_TRANSITION", f"指令状态 {old} 不允许迁移到 {new}（乱序回执不倒退）")

    # ---------- 池 ----------
    def create_pool(self, pool_id: str, budget: str) -> dict:
        def op(db):
            exists = db.execute("SELECT budget FROM m7_pools WHERE pool_id=?", (pool_id,)).fetchone()
            if exists:
                raise DomainError("DUPLICATE_POOL", f"资金池 {pool_id} 已存在")
            db.execute("INSERT INTO m7_pools(pool_id, budget, created_at) VALUES (?,?,?)",
                       (pool_id, budget, utc_now()))
            self.event(db, "pool", pool_id, "POOL_CREATED", {"budget": budget})
            return {"pool_id": pool_id, "budget": budget}
        return self.write(op)

    def get_pool(self, pool_id: str) -> dict:
        def op(db):
            row = db.execute("SELECT * FROM m7_pools WHERE pool_id=?", (pool_id,)).fetchone()
            if not row:
                raise DomainError("POOL_NOT_FOUND", f"资金池 {pool_id} 不存在")
            return dict(row)
        return self.read(op)

    def list_pools(self) -> list[dict]:
        def op(db):
            return [dict(r) for r in db.execute("SELECT pool_id, budget, created_at FROM m7_pools ORDER BY pool_id")]
        return self.read(op)

    # ---------- 批次与指令 ----------
    def create_batch(self, pool_id: str, batch_id: str, digest: str, total: str,
                     unallocated: str, instructions: list[dict], available_check) -> dict:
        def op(db):
            pool = db.execute("SELECT budget FROM m7_pools WHERE pool_id=?", (pool_id,)).fetchone()
            if not pool:
                raise DomainError("POOL_NOT_FOUND", f"资金池 {pool_id} 不存在")
            dup = db.execute("SELECT batch_id FROM m7_batches WHERE pool_id=? AND digest=?",
                             (pool_id, digest)).fetchone()
            if dup:
                raise DomainError("DUPLICATE_BATCH", f"同一输入摘要已存在批次 {dup[0]}")
            # 跨批次并发占款不超预算：在持锁事务内核对实时可用资金
            snapshot = self._instructions_for_pool(db, pool_id)
            available = available_check(snapshot, pool["budget"])
            if available < sum((Decimal(x["amount"]) for x in instructions), Decimal("0")):
                raise DomainError("BUDGET_EXCEEDED", "可用资金不足，跨批次占款不得超过预算")
            now = utc_now()
            db.execute(
                "INSERT INTO m7_batches(batch_id, pool_id, digest, total, unallocated, created_at) "
                "VALUES (?,?,?,?,?,?)", (batch_id, pool_id, digest, total, unallocated, now))
            for ins in instructions:
                db.execute(
                    "INSERT INTO m7_instructions(instruction_id, batch_id, project_id, amount, state, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (ins["instruction_id"], batch_id, ins["project_id"], ins["amount"], "PENDING", now))
            self.event(db, "batch", batch_id, "BATCH_CREATED",
                       {"pool_id": pool_id, "total": total, "count": len(instructions)})
            return {"batch_id": batch_id, "pool_id": pool_id, "digest": digest,
                    "total": total, "unallocated": unallocated, "instruction_count": len(instructions)}
        return self.write(op)

    def get_batch(self, batch_id: str) -> dict:
        def op(db):
            row = db.execute("SELECT * FROM m7_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not row:
                raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
            return dict(row)
        return self.read(op)

    def list_batches(self, pool_id: str | None = None) -> list[dict]:
        def op(db):
            if pool_id:
                rows = db.execute("SELECT * FROM m7_batches WHERE pool_id=? ORDER BY batch_id", (pool_id,))
            else:
                rows = db.execute("SELECT * FROM m7_batches ORDER BY batch_id")
            return [dict(r) for r in rows]
        return self.read(op)

    def list_instructions(self, batch_id: str | None = None, pool_id: str | None = None) -> list[dict]:
        def op(db):
            sql = ("SELECT i.* FROM m7_instructions i JOIN m7_batches b ON i.batch_id=b.batch_id "
                   "WHERE 1=1")
            args = []
            if batch_id:
                sql += " AND i.batch_id=?"; args.append(batch_id)
            if pool_id:
                sql += " AND b.pool_id=?"; args.append(pool_id)
            sql += " ORDER BY i.instruction_id"
            return [dict(r) for r in db.execute(sql, args)]
        return self.read(op)

    def _instructions_for_pool(self, db, pool_id: str) -> list[dict]:
        rows = db.execute(
            "SELECT i.* FROM m7_instructions i JOIN m7_batches b ON i.batch_id=b.batch_id "
            "WHERE b.pool_id=? ORDER BY i.instruction_id", (pool_id,))
        return [dict(r) for r in rows]

    def _get_instruction(self, db, instruction_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM m7_instructions WHERE instruction_id=?",
                         (instruction_id,)).fetchone()
        if not row:
            raise DomainError("INSTRUCTION_NOT_FOUND", f"待付指令 {instruction_id} 不存在")
        return row

    # ---------- 模拟支付端场景 ----------
    def set_scenario(self, key: str, scenario: str) -> dict:
        if scenario not in SCENARIOS:
            raise DomainError("INVALID_SCENARIO", f"未知模拟场景 {scenario}，可选 {sorted(SCENARIOS)}")
        def op(db):
            db.execute("INSERT INTO m7_scenarios(key, scenario) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET scenario=excluded.scenario", (key, scenario))
            self.event(db, "scenario", key, "SCENARIO_SET", {"scenario": scenario})
            return {"key": key, "scenario": scenario}
        return self.write(op)

    def get_scenario(self, db, instruction_id: str, batch_id: str) -> str:
        for key in (instruction_id, f"batch:{batch_id}", "default"):
            row = db.execute("SELECT scenario FROM m7_scenarios WHERE key=?", (key,)).fetchone()
            if row:
                return row[0]
        return "ok"

    def clear_interrupt_scenario(self, db, instruction_id: str) -> None:
        """中断只发生一次：首次受理后自动复位，后续重试走正常链路。"""
        db.execute("DELETE FROM m7_scenarios WHERE key=?", (instruction_id,))

    # ---------- 发送支付（含超时先查受理、中断恢复、不重复扣款） ----------
    def send_payment(self, instruction_id: str, request_id: str, on_available=None) -> dict:
        outcome = self.write(lambda db: self._send_payment(db, instruction_id, request_id))
        # 受理已提交落库后再向调用方发信号：中断/超时不抹掉受理，重连后可恢复且不重复扣款
        if outcome.get("signal") == "BANK_INTERRUPTED":
            raise DomainError("BANK_INTERRUPTED", "连接中断，支付端受理状态未知，请重新发送以查询恢复")
        if outcome.get("signal") == "BANK_TIMEOUT":
            raise DomainError("BANK_TIMEOUT", "支付端响应超时，请重新发送先查受理状态")
        outcome.pop("signal", None)
        return outcome

    def _send_payment(self, db, instruction_id: str, request_id: str) -> dict:
        ins = self._get_instruction(db, instruction_id)
        if ins["state"] not in ("PENDING", "SUBMITTED"):
            raise DomainError("INVALID_TRANSITION", f"指令 {instruction_id} 处于 {ins['state']}，不可发送支付")
        req = db.execute("SELECT * FROM m7_bank_requests WHERE instruction_id=? ORDER BY rowid",
                         (instruction_id,)).fetchone()
        now = utc_now()
        if req is None:
            # 首次发送：以 instruction_id 为幂等键，模拟支付端只认一个请求，绝不重复扣款
            scenario = self.get_scenario(db, instruction_id, ins["batch_id"])
            if scenario == "ok":
                bank_state = BANK_ACCEPTED
                new_ins_state = "SUBMITTED"
                self._guard_transition(ins["state"], new_ins_state)
                self._update_state(db, ins, new_ins_state, now)
            elif scenario == "reject":
                bank_state = BANK_FAILED
                new_ins_state = "FAILED"
                self._guard_transition(ins["state"], new_ins_state)
                self._update_state(db, ins, new_ins_state, now)
            elif scenario == "timeout":
                # 支付端其实已受理，但调用方收到超时：只留受理记录，指令保持可重试
                bank_state = BANK_ACCEPTED
                new_ins_state = ins["state"]
            else:  # interrupt：受理落库后连接中断，调用方无响应；场景自动复位以便恢复
                bank_state = BANK_ACCEPTED
                new_ins_state = ins["state"]
                self.clear_interrupt_scenario(db, instruction_id)
            db.execute(
                "INSERT INTO m7_bank_requests(request_id, instruction_id, amount, scenario, state, attempts, created_at, updated_at) "
                "VALUES (?,?,?,?,?,1,?,?)",
                (request_id, instruction_id, ins["amount"], scenario, bank_state, now, now))
            self.event(db, "instruction", instruction_id, "PAYMENT_SENT",
                       {"request_id": request_id, "scenario": scenario, "bank_state": bank_state,
                        "instruction_state": ins["state"]})
            if scenario == "interrupt":
                return {"instruction_id": instruction_id, "request_id": request_id,
                        "bank_state": bank_state, "instruction_state": new_ins_state,
                        "scenario": scenario, "attempts": 1, "signal": "BANK_INTERRUPTED"}
            if scenario == "timeout":
                return {"instruction_id": instruction_id, "request_id": request_id,
                        "bank_state": bank_state, "instruction_state": new_ins_state,
                        "scenario": scenario, "attempts": 1, "signal": "BANK_TIMEOUT"}
            return {"instruction_id": instruction_id, "request_id": request_id,
                    "bank_state": bank_state, "instruction_state": new_ins_state,
                    "scenario": scenario, "attempts": 1}
        # 重试路径：先查受理状态，已受理直接复用，绝不再发一笔
        db.execute("UPDATE m7_bank_requests SET attempts=attempts+1, updated_at=? WHERE request_id=?",
                   (now, req["request_id"]))
        self.event(db, "instruction", instruction_id, "PAYMENT_RETRY",
                   {"request_id": req["request_id"], "bank_state": req["state"], "attempts": req["attempts"] + 1})
        if req["state"] == BANK_ACCEPTED and ins["state"] == "PENDING":
            self._update_state(db, ins, "SUBMITTED", now)
        if req["state"] == BANK_FAILED:
            self._update_state(db, ins, "FAILED", now)
        return {"instruction_id": instruction_id, "request_id": req["request_id"],
                "bank_state": req["state"], "instruction_state":
                    ("SUBMITTED" if req["state"] == BANK_ACCEPTED else "FAILED"),
                "scenario": req["scenario"], "attempts": req["attempts"] + 1,
                "reused_acceptance": True}

    def _update_state(self, db, ins, new_state: str, now: str) -> None:
        self._guard_transition(ins["state"], new_state)
        db.execute("UPDATE m7_instructions SET state=?, version=version+1, updated_at=? WHERE instruction_id=?",
                   (new_state, now, ins["instruction_id"]))
        self.event(db, "instruction", ins["instruction_id"], "STATE_CHANGED",
                   {"from": ins["state"], "to": new_state})
        ins_obj = dict(ins)
        ins_obj["state"] = new_state
        return ins_obj

    def get_bank_status(self, instruction_id: str) -> dict:
        def op(db):
            self._get_instruction(db, instruction_id)
            req = db.execute("SELECT * FROM m7_bank_requests WHERE instruction_id=? ORDER BY rowid",
                             (instruction_id,)).fetchone()
            if not req:
                return {"instruction_id": instruction_id, "accepted": False, "bank_state": None, "attempts": 0}
            return {"instruction_id": instruction_id, "accepted": True,
                    "bank_state": req["state"], "request_id": req["request_id"],
                    "scenario": req["scenario"], "attempts": req["attempts"]}
        return self.read(op)

    # ---------- 回执（乱序不倒退，幂等） ----------
    def post_receipt(self, instruction_id: str, outcome: str) -> dict:
        if outcome not in (BANK_SUCCESS, BANK_FAILED):
            raise DomainError("INVALID_RECEIPT", f"回执结果只能是 {BANK_SUCCESS} 或 {BANK_FAILED}")
        def op(db):
            ins = self._get_instruction(db, instruction_id)
            req = db.execute("SELECT * FROM m7_bank_requests WHERE instruction_id=? ORDER BY rowid",
                             (instruction_id,)).fetchone()
            if not req:
                raise DomainError("NO_ACCEPTANCE", f"指令 {instruction_id} 无支付端受理记录，回执不予入账")
            now = utc_now()
            if outcome == BANK_SUCCESS:
                target = "RECOVERY_PENDING" if ins["withdraw_flag"] else "PAID"
                if ins["state"] == target:
                    self.event(db, "instruction", instruction_id, "RECEIPT_DUPLICATE", {"outcome": outcome})
                    return {"instruction_id": instruction_id, "state": ins["state"], "idempotent": True}
                if ins["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED"):
                    # 迟到/乱序成功：已在实付轨道，不倒退、不重复记账
                    self.event(db, "instruction", instruction_id, "RECEIPT_OUT_OF_ORDER",
                               {"outcome": outcome, "state": ins["state"]})
                    return {"instruction_id": instruction_id, "state": ins["state"], "idempotent": True}
                self._guard_transition(ins["state"], target)
                self._update_state(db, ins, target, now)
                db.execute("UPDATE m7_bank_requests SET state=?, updated_at=? WHERE request_id=?",
                           (BANK_SUCCESS, now, req["request_id"]))
                self.event(db, "instruction", instruction_id, "RECEIPT_SUCCESS",
                           {"to": target, "withdraw_flag": bool(ins["withdraw_flag"])})
                return {"instruction_id": instruction_id, "state": target, "idempotent": False}
            # 失败回执
            if ins["state"] == "FAILED":
                self.event(db, "instruction", instruction_id, "RECEIPT_DUPLICATE", {"outcome": outcome})
                return {"instruction_id": instruction_id, "state": "FAILED", "idempotent": True}
            if ins["state"] in ("PAID", "RECOVERY_PENDING", "RECOVERED"):
                raise DomainError("RECEIPT_CONFLICT", "已实付指令收到失败回执，拒绝倒退入账")
            self._guard_transition(ins["state"], "FAILED")
            self._update_state(db, ins, "FAILED", now)
            db.execute("UPDATE m7_bank_requests SET state=?, updated_at=? WHERE request_id=?",
                       (BANK_FAILED, now, req["request_id"]))
            self.event(db, "instruction", instruction_id, "RECEIPT_FAILED", {"to": "FAILED"})
            return {"instruction_id": instruction_id, "state": "FAILED", "idempotent": False}
        return self.write(op)

    # ---------- 撤回（作废待付，实付保留转待追回） ----------
    def withdraw_batch(self, batch_id: str, instruction_ids: list[str] | None = None) -> dict:
        def op(db):
            batch = db.execute("SELECT * FROM m7_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
            rows = db.execute("SELECT * FROM m7_instructions WHERE batch_id=? ORDER BY instruction_id",
                              (batch_id,)).fetchall()
            if instruction_ids is not None:
                wanted = set(instruction_ids)
                rows = [r for r in rows if r["instruction_id"] in wanted]
                if len(rows) != len(wanted):
                    raise DomainError("INSTRUCTION_NOT_FOUND", "部分待付指令不属于该批次或不存在")
            now = utc_now()
            actions = []
            for row in rows:
                state = row["state"]
                if state == "PENDING":
                    # 支付端未受理：直接作废待付指令，释放占款
                    self._guard_transition(state, "VOIDED")
                    self._update_state(db, row, "VOIDED", now)
                    actions.append({"instruction_id": row["instruction_id"], "action": "VOIDED", "from": state})
                elif state == "SUBMITTED":
                    # 已受理未出回执：不能直接撤，标记撤回；成功回执到达时如实入账并转待追回
                    if not row["withdraw_flag"]:
                        db.execute("UPDATE m7_instructions SET withdraw_flag=1, version=version+1, updated_at=? "
                                   "WHERE instruction_id=?", (now, row["instruction_id"]))
                        self.event(db, "instruction", row["instruction_id"], "WITHDRAW_MARKED", {"state": state})
                    actions.append({"instruction_id": row["instruction_id"], "action": "MARKED_WITHDRAW", "from": state})
                elif state in ("PAID",):
                    self._guard_transition(state, "RECOVERY_PENDING")
                    self._update_state(db, row, "RECOVERY_PENDING", now)
                    actions.append({"instruction_id": row["instruction_id"], "action": "RECOVERY_PENDING", "from": state})
                elif state == "RECOVERY_PENDING":
                    actions.append({"instruction_id": row["instruction_id"], "action": "RECOVERY_PENDING", "from": state})
                else:
                    # FAILED / VOIDED / RECOVERED 为终态，撤回不改变实付事实
                    actions.append({"instruction_id": row["instruction_id"], "action": "UNCHANGED", "from": state})
            db.execute("UPDATE m7_batches SET withdrawn=1, withdrawn_at=? WHERE batch_id=?", (now, batch_id))
            self.event(db, "batch", batch_id, "BATCH_WITHDRAWN",
                       {"count": len(rows), "actions": actions})
            return {"batch_id": batch_id, "withdrawn": True, "actions": actions}
        return self.write(op)

    # ---------- 追回登记（全额到账才释放，未到账不释放给其他批次） ----------
    def register_recovery(self, instruction_id: str, amount: str) -> dict:
        def op(db):
            ins = self._get_instruction(db, instruction_id)
            if ins["state"] != "RECOVERY_PENDING":
                raise DomainError("NOT_RECOVERABLE", f"指令 {instruction_id} 处于 {ins['state']}，不可登记追回")
            if Decimal(amount) != Decimal(ins["amount"]):
                raise DomainError("RECOVERY_AMOUNT_MISMATCH",
                                  f"追回款必须与实付 {ins['amount']} 全额一致，部分到账不予释放")
            now = utc_now()
            self._guard_transition(ins["state"], "RECOVERED")
            self._update_state(db, ins, "RECOVERED", now)
            self.event(db, "instruction", instruction_id, "RECOVERY_REGISTERED", {"amount": amount})
            return {"instruction_id": instruction_id, "state": "RECOVERED", "amount": amount}
        return self.write(op)

    # ---------- 事件与对账 ----------
    def list_events(self, ref_id: str | None = None) -> list[dict]:
        def op(db):
            if ref_id:
                rows = db.execute("SELECT * FROM m7_events WHERE ref_id=? ORDER BY seq", (ref_id,))
            else:
                rows = db.execute("SELECT * FROM m7_events ORDER BY seq")
            return [dict(r) for r in rows]
        return self.read(op)
