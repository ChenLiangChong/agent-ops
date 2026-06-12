"""組織記憶：兩層式（Letta blocks ＋ mem0 facts）＋ 注入歸因。

- memory_blocks：小而常駐，每個角色的「人格＋鐵律」，每次派工全注入。
- memory_facts：大而檢索，top-K 注入，複合分數 = 相似 × 新近衰減 × 重要性（CrewAI 配方）。
- 注入了哪些 facts 記在 runs.injected_memory_ids → 之後人類回饋能歸因到具體記憶。
  這條歸因鏈就是「越用越像員工」的閉環（Shopify 沒公開做到的部分）。
- 檢索離線可跑：token overlap，不依賴 embedding；要升級換 FTS5/向量即可，介面不變。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass


@dataclass
class Injection:
    text: str
    fact_ids: list[int]


def set_block(db: sqlite3.Connection, agent_id: str | None, label: str, value: str,
              char_limit: int = 2000) -> None:
    if len(value) > char_limit:
        raise ValueError(f"block {label} 超過 char_limit {char_limit}")
    with db:
        db.execute(
            """INSERT INTO memory_blocks (agent_id, label, value, char_limit)
               VALUES (?,?,?,?)
               ON CONFLICT (agent_id, label)
               DO UPDATE SET value=excluded.value, version=version+1""",
            (agent_id, label, value, char_limit),
        )


def add_fact(
    db: sqlite3.Connection,
    fact: str,
    source_type: str,
    actor: str,
    source_quote: str | None = None,
    scope_agent: str | None = None,
    scope_repo: str | None = None,
    scope_person: str | None = None,
    category: str | None = None,
    importance: float = 0.5,
    provenance_run: int | None = None,
) -> int:
    h = _hash(fact)
    with db:
        cur = db.execute(
            """INSERT INTO memory_facts
               (fact, hash, scope_agent, scope_repo, scope_person, category,
                importance, source_type, source_quote, provenance_run)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fact, h, scope_agent, scope_repo, scope_person, category,
             importance, source_type, source_quote, provenance_run),
        )
        fact_id = cur.lastrowid
        db.execute(
            "INSERT INTO memory_history (memory_id, old_value, new_value, event, actor) "
            "VALUES (?,NULL,?,?,?)",
            (fact_id, fact, "ADD", actor),
        )
    return fact_id


def supersede_fact(db: sqlite3.Connection, old_id: int, new_fact: str, actor: str,
                   **kwargs) -> int:
    old = db.execute("SELECT * FROM memory_facts WHERE id=?", (old_id,)).fetchone()
    if old is None:
        raise ValueError(f"fact #{old_id} 不存在")
    new_id = add_fact(
        db, new_fact,
        source_type=kwargs.get("source_type", old["source_type"]),
        actor=actor,
        source_quote=kwargs.get("source_quote", old["source_quote"]),
        scope_agent=kwargs.get("scope_agent", old["scope_agent"]),
        scope_repo=kwargs.get("scope_repo", old["scope_repo"]),
        category=kwargs.get("category", old["category"]),
        importance=kwargs.get("importance", old["importance"]),
        provenance_run=kwargs.get("provenance_run"),
    )
    with db:
        db.execute("UPDATE memory_facts SET superseded_by=? WHERE id=?", (new_id, old_id))
        db.execute(
            "INSERT INTO memory_history (memory_id, old_value, new_value, event, actor) "
            "VALUES (?,?,?,?,?)",
            (old_id, old["fact"], new_fact, "UPDATE", actor),
        )
    return new_id


def retrieve(db: sqlite3.Connection, query: str, k: int = 5,
             scope_agent: str | None = None, scope_repo: str | None = None) -> list[sqlite3.Row]:
    """top-K：相似（token overlap）× 新近衰減（半衰期 30 天）× 重要性。只取未被取代的。"""
    rows = db.execute(
        """SELECT *, julianday('now') - julianday(created_at) AS age_days
           FROM memory_facts
           WHERE superseded_by IS NULL
             AND (scope_agent IS NULL OR scope_agent = ?)
             AND (scope_repo IS NULL OR scope_repo = ?)""",
        (scope_agent, scope_repo),
    ).fetchall()
    q_tokens = _tokens(query)
    scored = []
    for r in rows:
        sim = _overlap(q_tokens, _tokens(r["fact"]))
        if sim <= 0:
            continue
        recency = math.exp(-math.log(2) * (r["age_days"] or 0) / 30.0)
        scored.append((sim * recency * r["importance"], r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]


def compose_injection(db: sqlite3.Connection, agent_id: str, query: str,
                      scope_repo: str | None = None, k: int = 5) -> Injection:
    """派工時組 prompt 的記憶段：角色 blocks（全注入）＋ top-K facts（附出處）。"""
    blocks = db.execute(
        "SELECT label, value FROM memory_blocks WHERE agent_id IS NULL OR agent_id=? ORDER BY label",
        (agent_id,),
    ).fetchall()
    facts = retrieve(db, query, k=k, scope_agent=agent_id, scope_repo=scope_repo)
    lines = [f"[{b['label']}] {b['value']}" for b in blocks]
    for f in facts:
        src = f" (出處: {f['source_quote'][:60]}…)" if f["source_quote"] else ""
        lines.append(f"[記憶#{f['id']}|{f['category'] or 'fact'}] {f['fact']}{src}")
    return Injection(text="\n".join(lines), fact_ids=[f["id"] for f in facts])


def export_review_rules(db: sqlite3.Connection, path: str, scope_repo: str | None = None) -> int:
    """把組織記憶投影成 review skill 可讀的 markdown，給 CI 第二道 leader review 載入。

    這就是記憶飛輪接到 CI 的那條線：人在 PR 上教平台的規矩 → 蒸餾成 memory_facts →
    投影成這個檔 → leader review 拿來查「過去被糾正過的問題有沒有重蹈覆轍」。
    """
    import os

    rows = db.execute(
        """SELECT * FROM memory_facts
           WHERE superseded_by IS NULL AND (? IS NULL OR scope_repo IS NULL OR scope_repo = ?)
           ORDER BY importance DESC, id""",
        (scope_repo, scope_repo),
    ).fetchall()

    lines = [
        "# 組織記憶 → review 規矩",
        "",
        "> 自動投影，**不要手改**。由 `agentops export-review-rules` 從平台 `memory_facts` 產生，",
        "> 隨記憶飛輪更新。第二道 leader review（`.github/workflows/leader-review.yml`）載入它，",
        "> 查 PR 有沒有重蹈過去被糾正過的問題。每條附出處（source_quote）。",
        "",
    ]
    if not rows:
        lines.append("（還沒有累積的規矩——有人在 PR 上糾正、平台蒸餾後這裡會長出來。）")
    else:
        by_cat: dict[str, list] = {}
        for r in rows:
            by_cat.setdefault(r["category"] or "其他", []).append(r)
        for cat, items in by_cat.items():
            lines.append(f"## {cat}")
            for r in items:
                scope = f" `[{r['scope_agent']}]`" if r["scope_agent"] else ""
                src = f"　_出處：「{r['source_quote']}」_" if r["source_quote"] else ""
                lines.append(f"- **#{r['id']}**{scope} {r['fact']}{src}")
            lines.append("")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(rows)


def _hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode()).hexdigest()[:16]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokens(text: str) -> set[str]:
    # 中英混合：英數連續段 + 單一 CJK 字 + CJK bigram，離線無依賴
    ascii_tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    cjk = re.findall(r"[一-鿿]", text)
    bigrams = {a + b for a, b in zip(cjk, cjk[1:])}
    return ascii_tokens | set(cjk) | bigrams


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))
