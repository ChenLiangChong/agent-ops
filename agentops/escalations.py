"""通知（不擋路）：硬接線 enqueue ＋ 笨投遞器。

設計出處 sme-ai-kit 的三條鐵律：
1. enqueue 跟業務寫入在**同一 transaction**——agent 看不到、跳不過、改不掉。
2. actor 與收件人在 enqueue 時由系統蓋章定死，投遞器永不重算身分。
3. 投遞器是「笨」的：claim-lease（CAS＋TTL）防多投遞層重複發送，實際送出的文字進稽核。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

CLAIM_TTL_MIN = 10

# sender(target, text) -> bool；真實版換成 Slack/LINE push，mock 版印 terminal
Sender = Callable[[str, str], bool]


def enqueue_in_tx(
    db: sqlite3.Connection,
    event_type: str,
    summary: str,
    detail: str | None,
    actor: str,
    target: str,
) -> int:
    """必須在呼叫端已開的 transaction 內使用（跟業務寫入同生死）。"""
    cur = db.execute(
        """INSERT INTO escalations (event_type, summary, detail, actor, target)
           VALUES (?,?,?,?,?)""",
        (event_type, summary, detail, actor, target),
    )
    return cur.lastrowid


def flush(db: sqlite3.Connection, send: Sender, max_retry: int = 3) -> dict:
    """投遞 pending：先租約 claim（獨立 transaction），再送（網路 I/O 不持鎖），再標記。"""
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    while True:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM escalations
               WHERE status='pending'
                 AND (claimed_at IS NULL
                      OR datetime(claimed_at, '+' || ? || ' minutes') < datetime('now'))
               ORDER BY id LIMIT 1""",
            (CLAIM_TTL_MIN,),
        ).fetchone()
        if row is None:
            db.execute("ROLLBACK")
            break
        db.execute("UPDATE escalations SET claimed_at=datetime('now') WHERE id=?", (row["id"],))
        db.execute("COMMIT")

        text = f"[{row['event_type']}] {row['summary']}"
        ok = False
        try:
            ok = send(row["target"], text)
        except Exception:
            ok = False

        with db:
            if ok:
                db.execute(
                    "UPDATE escalations SET status='sent', sent_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
                # 稽核存「實際送出的文字」，不是模板
                db.execute(
                    "INSERT INTO interaction_log (actor, action, target_type, target_id, detail) "
                    "VALUES (?,?,?,?,?)",
                    (row["actor"], "escalation_sent", "escalation", str(row["id"]), text),
                )
                stats["sent"] += 1
            else:
                retry = row["retry_count"] + 1
                status = "failed" if retry >= max_retry else "pending"
                db.execute(
                    "UPDATE escalations SET retry_count=?, status=?, claimed_at=NULL WHERE id=?",
                    (retry, status, row["id"]),
                )
                stats["failed" if status == "failed" else "skipped"] += 1
    return stats
