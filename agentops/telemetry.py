"""遙測：每次 agent 執行都留 runs／tool_calls／成本。

Shopify 姿勢：花費「告警不設上限」（單日超閾值發 escalation，不擋人）。
欄位命名對齊 OTel GenAI 慣例（operation_name / provider_name / *_tokens）。
大件（transcript）落檔，DB 只存路徑。
"""
from __future__ import annotations

import json
import sqlite3

DAILY_SPEND_ALERT_USD = 250.0   # Shopify 同款閾值


def start_run(
    db: sqlite3.Connection,
    task_id: int,
    agent_id: str,
    stage: str | None,
    auth_mode: str,
    injected_memory_ids: list[int] | None = None,
    parent_run_id: int | None = None,
    model: str | None = None,
) -> int:
    with db:
        cur = db.execute(
            """INSERT INTO runs (task_id, agent_id, stage, parent_run_id, auth_mode,
                                 request_model, provider_name, injected_memory_ids)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task_id, agent_id, stage, parent_run_id, auth_mode,
             model, "anthropic" if auth_mode != "none" else None,
             json.dumps(injected_memory_ids or [])),
        )
    return cur.lastrowid


def end_run(
    db: sqlite3.Connection,
    run_id: int,
    status: str = "ok",
    outcome: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error_type: str | None = None,
    transcript_path: str | None = None,
) -> None:
    with db:
        db.execute(
            """UPDATE runs SET status=?, outcome=?, error_type=?,
                   ended_at=datetime('now'),
                   duration_ms=CAST((julianday(datetime('now')) - julianday(started_at)) * 86400000 AS INTEGER),
                   input_tokens=?, output_tokens=?, cost_usd=?, transcript_path=?
               WHERE id=?""",
            (status, outcome, error_type, input_tokens, output_tokens,
             cost_usd, transcript_path, run_id),
        )


def log_tool_call(db: sqlite3.Connection, run_id: int, idx: int, tool_name: str,
                  success: bool, duration_ms: int = 0, payload_hash: str | None = None) -> None:
    with db:
        db.execute(
            "INSERT INTO tool_calls (run_id, idx, tool_name, success, duration_ms, payload_hash) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, idx, tool_name, int(success), duration_ms, payload_hash),
        )


def check_spend_alert(db: sqlite3.Connection, threshold_usd: float = DAILY_SPEND_ALERT_USD) -> float | None:
    """今日 API 花費超閾值 → 發 escalation（告警，不擋）。訂閱模式 cost_usd=0 自然不觸發。"""
    from . import escalations
    row = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS spend FROM runs WHERE date(started_at)=date('now')"
    ).fetchone()
    spend = row["spend"]
    if spend > threshold_usd:
        with db:
            escalations.enqueue_in_tx(
                db, "spend_alert",
                f"今日 API 花費 ${spend:.2f} 超過告警閾值 ${threshold_usd:.0f}（不擋，請留意）",
                None, actor="system", target="ops-channel",
            )
        return spend
    return None


def summary(db: sqlite3.Connection) -> dict:
    r = db.execute(
        """SELECT COUNT(*) AS runs,
                  COALESCE(SUM(input_tokens),0) AS tin,
                  COALESCE(SUM(output_tokens),0) AS tout,
                  COALESCE(SUM(cost_usd),0) AS cost,
                  SUM(CASE WHEN auth_mode='subscription' THEN 1 ELSE 0 END) AS sub_runs
           FROM runs"""
    ).fetchone()
    t = db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done FROM tasks"
    ).fetchone()
    return {
        "tasks": t["total"], "tasks_done": t["done"] or 0,
        "runs": r["runs"], "input_tokens": r["tin"], "output_tokens": r["tout"],
        "cost_usd": round(r["cost"], 4), "subscription_runs": r["sub_runs"] or 0,
    }


def recent_runs(db: sqlite3.Connection, limit: int = 10) -> list:
    """最近 N 次執行（dashboard 用）。"""
    return db.execute(
        "SELECT id, agent_id, stage, status, outcome, duration_ms, cost_usd "
        "FROM runs ORDER BY id DESC LIMIT " + str(limit)
    ).fetchall()
