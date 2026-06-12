"""審批閘門（擋路的閘）：resume_params 逐欄位綁定＋單次消費＋72h 過期。

整組搬 sme-ai-kit 的設計，把目標從「DB 寫入」改成「merge PR／publish twin／deploy」。
跟 escalations（通知不擋路）是不同物種，不可混用。

核心安全性：核准的是「這個動作＋這組參數」，不是「這個 agent」。
消費時逐欄位 exact-match（型別敏感），agent 拿到核准後想換參數執行 → 直接拒絕。
"""
from __future__ import annotations

import json
import sqlite3

from . import escalations

EXPIRES_HOURS = 72


class ApprovalError(Exception):
    pass


def request(
    db: sqlite3.Connection,
    type_: str,
    summary: str,
    resume_action: str,
    resume_params: dict,
    requester_run: int | None,
    notify_target: str = "ops-channel",
) -> int:
    """建審批單。escalation 在同一 transaction enqueue——agent 看不到也跳不過。"""
    with db:
        cur = db.execute(
            """INSERT INTO approvals (type, requester_run, summary, resume_action, resume_params, expires_at)
               VALUES (?,?,?,?,?, datetime('now', '+' || ? || ' hours'))""",
            (type_, requester_run, summary, resume_action,
             json.dumps(resume_params, ensure_ascii=False, sort_keys=True), EXPIRES_HOURS),
        )
        approval_id = cur.lastrowid
        escalations.enqueue_in_tx(
            db, "approval_pending",
            f"審批待決 #{approval_id}: {summary}",
            json.dumps({"approval_id": approval_id, "action": resume_action}, ensure_ascii=False),
            actor="system", target=notify_target,
        )
    return approval_id


def decide(db: sqlite3.Connection, approval_id: int, approver: str, approve: bool) -> None:
    status = "approved" if approve else "rejected"
    with db:
        n = db.execute(
            """UPDATE approvals SET status=?, approver=?, decided_at=datetime('now')
               WHERE id=? AND status='waiting' AND expires_at > datetime('now')""",
            (status, approver, approval_id),
        ).rowcount
        if n == 0:
            raise ApprovalError(f"approval #{approval_id} 不在可決狀態（已決/已過期/不存在）")
        db.execute(
            "INSERT INTO interaction_log (actor, action, target_type, target_id) VALUES (?,?,?,?)",
            (approver, f"approval_{status}", "approval", str(approval_id)),
        )


def _params_match(expected: dict, actual: dict) -> bool:
    """逐欄位、型別敏感比對。bool 先擋（bool 是 int 的子類）、數值同型比、其餘嚴格相等。"""
    if set(expected.keys()) != set(actual.keys()):
        return False
    for k, ev in expected.items():
        av = actual[k]
        if isinstance(ev, bool) or isinstance(av, bool):
            if not (isinstance(ev, bool) and isinstance(av, bool) and ev == av):
                return False
        elif isinstance(ev, (int, float)) and isinstance(av, (int, float)):
            if type(ev) is not type(av) or ev != av:
                return False
        elif type(ev) is not type(av) or ev != av:
            return False
    return True


def consume(
    db: sqlite3.Connection,
    approval_id: int,
    action: str,
    params: dict,
    run_id: int | None,
) -> None:
    """單次消費。動作或任一參數不符 → 拒絕並留稽核。
    稽核寫在 ROLLBACK 之後的獨立 transaction，否則會跟著回滾消失。"""
    tamper_detail = None
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"approval #{approval_id} 不存在")
        if row["status"] != "approved":
            raise ApprovalError(f"approval #{approval_id} 狀態 {row['status']}，不可消費")
        if row["consumed_at"] is not None:
            raise ApprovalError(f"approval #{approval_id} 已消費過（單次消費）")
        if row["expires_at"] <= _now(db):
            raise ApprovalError(f"approval #{approval_id} 已過期")
        if action != row["resume_action"]:
            tamper_detail = f"action 不符: {action} != {row['resume_action']}"
            raise ApprovalError(f"approval #{approval_id} 動作不符，拒絕")
        if not _params_match(json.loads(row["resume_params"]), params):
            tamper_detail = "resume_params 不符"
            raise ApprovalError(f"approval #{approval_id} 參數與核准內容不符，拒絕（防掉包）")
        db.execute(
            "UPDATE approvals SET consumed_at=datetime('now'), consumed_by_run=? WHERE id=?",
            (run_id, approval_id),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        if tamper_detail:
            with db:
                _audit_tamper(db, approval_id, tamper_detail)
        raise


def expire_stale(db: sqlite3.Connection) -> int:
    """boot 時清過期單（sme-ai-kit 的開機禮儀）。"""
    with db:
        return db.execute(
            "UPDATE approvals SET status='expired' WHERE status='waiting' AND expires_at <= datetime('now')"
        ).rowcount


def _now(db: sqlite3.Connection) -> str:
    return db.execute("SELECT datetime('now') AS t").fetchone()["t"]


def _audit_tamper(db: sqlite3.Connection, approval_id: int, detail: str) -> None:
    db.execute(
        "INSERT INTO interaction_log (actor, action, target_type, target_id, detail) VALUES (?,?,?,?,?)",
        ("system", "approval_tamper_rejected", "approval", str(approval_id), detail),
    )
