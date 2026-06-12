"""回饋蒸餾：feedback_events → memory_facts（mem0 兩段式：抽取 → 對帳）。

人類訊號（review 意見、修改 diff、merge 與否、審批駁回）是原料；
蒸餾出的 fact 必附 source_quote（原話）——反捏造由 DB 層 CHECK 強制。

離線版（DeterministicDistiller）用啟發式抽取；生產版換 claude -p（訂閱）做抽取與對帳，
介面不變。對帳規則：hash 相同 → 跳過；token overlap 過閾 → supersede（UPDATE）；否則 ADD。
"""
from __future__ import annotations

import sqlite3

from . import memory

SIMILAR_THRESHOLD = 0.5   # 離線 token-overlap 的經驗值；生產版換 LLM 對帳，此值不再使用

_CATEGORY_HINTS = [
    ("convention", ["要用", "為準", "規範", "命名", "格式", "慣例", "不要用", "不可", "必須"]),
    ("preference", ["希望", "偏好", "喜歡", "請改成"]),
    ("decision", ["決定", "拍板", "定案"]),
    ("gotcha", ["注意", "坑", "陷阱", "bug", "踩過"]),
]


def _guess_category(text: str) -> str:
    for cat, kws in _CATEGORY_HINTS:
        if any(k in text for k in kws):
            return cat
    return "convention"


class DeterministicDistiller:
    """離線啟發式：一則回饋 → 一條候選 fact（修剪客套話）。生產換 LLM 抽取，介面同。"""

    def extract(self, content: str) -> list[str]:
        text = content.strip()
        return [text] if text else []


def distill_pending(
    db: sqlite3.Connection,
    extractor=None,
    scope_agent: str | None = None,
    scope_repo: str | None = None,
) -> list[int]:
    """跑所有未蒸餾的 feedback_events，回傳新增/更新的 fact ids。"""
    extractor = extractor or DeterministicDistiller()
    new_ids: list[int] = []
    rows = db.execute("SELECT * FROM feedback_events WHERE distilled=0 ORDER BY id").fetchall()
    for fb in rows:
        for candidate in extractor.extract(fb["content"]):
            fact_id = _reconcile(db, candidate, fb, scope_agent, scope_repo)
            if fact_id:
                new_ids.append(fact_id)
        with db:
            db.execute("UPDATE feedback_events SET distilled=1 WHERE id=?", (fb["id"],))
    return new_ids


def _reconcile(db, candidate: str, fb: sqlite3.Row,
               scope_agent: str | None, scope_repo: str | None) -> int | None:
    """mem0 第二段：跟既有記憶對帳，決定 ADD / UPDATE / 跳過。"""
    h = memory._hash(candidate)
    exact = db.execute(
        "SELECT id FROM memory_facts WHERE hash=? AND superseded_by IS NULL", (h,)
    ).fetchone()
    if exact:
        return None  # 一模一樣，不重複記

    cand_tokens = memory._tokens(candidate)
    for row in db.execute(
        "SELECT * FROM memory_facts WHERE superseded_by IS NULL"
    ).fetchall():
        if memory._overlap(cand_tokens, memory._tokens(row["fact"])) >= SIMILAR_THRESHOLD:
            return memory.supersede_fact(
                db, row["id"], candidate, actor=fb["author"],
                source_quote=fb["content"], provenance_run=fb["run_id"],
            )

    # 人類直接說的 → explicit（必附原話）；merge/rating 等行為訊號 → observed
    source_type = "explicit" if fb["kind"] in ("review_comment", "rejection") else "observed"
    return memory.add_fact(
        db, candidate,
        source_type=source_type,
        actor=fb["author"],
        source_quote=fb["content"] if source_type == "explicit" else None,
        scope_agent=scope_agent, scope_repo=scope_repo,
        category=_guess_category(candidate),
        importance=0.7 if source_type == "explicit" else 0.5,
        provenance_run=fb["run_id"],
    )


def record_feedback(
    db: sqlite3.Connection,
    kind: str,
    author: str,        # verified actor（由 adapter 驗證後傳入）
    content: str,
    run_id: int | None = None,
    artifact_id: int | None = None,
) -> int:
    with db:
        cur = db.execute(
            "INSERT INTO feedback_events (run_id, artifact_id, kind, author, content) "
            "VALUES (?,?,?,?,?)",
            (run_id, artifact_id, kind, author, content),
        )
    return cur.lastrowid
