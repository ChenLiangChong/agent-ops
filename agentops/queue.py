"""工單佇列：claim-lease（CAS ＋ TTL），搬 sme-ai-kit pending_escalations 的派工原語。

多個 worker 併發 claim 不重複；worker 死掉 → 租約逾時自動回收重派。
"""
from __future__ import annotations

import json
import sqlite3


def enqueue(
    db: sqlite3.Connection,
    workflow: str,
    title: str,
    payload: dict,
    source: str,
    requested_by: str,
    external_ref: str | None = None,
) -> int:
    with db:
        cur = db.execute(
            """INSERT INTO tasks (workflow, title, payload, source, requested_by, external_ref)
               VALUES (?,?,?,?,?,?)""",
            (workflow, title, json.dumps(payload, ensure_ascii=False), source, requested_by, external_ref),
        )
        db.execute(
            "INSERT INTO interaction_log (actor, action, target_type, target_id) VALUES (?,?,?,?)",
            (requested_by, "task_enqueued", "task", str(cur.lastrowid)),
        )
    return cur.lastrowid


def claim(db: sqlite3.Connection, agent_id: str) -> sqlite3.Row | None:
    """原子認領：queued、或 claimed 但租約過期的單。BEGIN IMMEDIATE 防兩個 worker 搶同單。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            """SELECT id FROM tasks
               WHERE status='queued'
                  OR (status='claimed'
                      AND datetime(claimed_at, '+' || claim_ttl_sec || ' seconds') < datetime('now'))
               ORDER BY id LIMIT 1"""
        ).fetchone()
        if row is None:
            db.execute("ROLLBACK")
            return None
        db.execute(
            """UPDATE tasks SET status='claimed', assignee_agent=?, claimed_at=datetime('now')
               WHERE id=?""",
            (agent_id, row["id"]),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()


def set_status(db: sqlite3.Connection, task_id: int, status: str) -> None:
    done_at = ", done_at=datetime('now')" if status in ("done", "failed", "cancelled") else ""
    with db:
        db.execute(f"UPDATE tasks SET status=?{done_at} WHERE id=?", (status, task_id))
