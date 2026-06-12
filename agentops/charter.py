"""角色 charter 載入：charters/*.md（frontmatter ＋ 行為準則本文）。

格式承襲 sme-ai-kit 的 agent 定義模式：身分／鐵律／輸出格式／blocker 條件。
charter 是資料不是程式——加一個角色 = 加一個 .md，不改 code。
init 時 upsert 進 agents 表。
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Charter:
    id: str
    role: str
    path: str
    auth_mode: str = "subscription"
    model: str | None = None
    body: str = ""
    meta: dict = field(default_factory=dict)


def parse(path: str) -> Charter:
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: 缺 frontmatter")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    cid = meta.get("id") or os.path.splitext(os.path.basename(path))[0]
    return Charter(
        id=cid,
        role=meta.get("role", cid),
        path=path,
        auth_mode=meta.get("auth_mode", "subscription"),
        model=meta.get("model") or None,
        body=m.group(2).strip(),
        meta=meta,
    )


def load_all(charters_dir: str) -> list[Charter]:
    out = []
    for name in sorted(os.listdir(charters_dir)):
        if name.endswith(".md"):
            out.append(parse(os.path.join(charters_dir, name)))
    return out


def register(db: sqlite3.Connection, charters: list[Charter]) -> None:
    with db:
        for c in charters:
            db.execute(
                """INSERT INTO agents (id, role, charter_path, model, auth_mode)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT (id) DO UPDATE
                   SET role=excluded.role, charter_path=excluded.charter_path,
                       model=excluded.model, auth_mode=excluded.auth_mode,
                       version=version+1""",
                (c.id, c.role, c.path, c.model, c.auth_mode),
            )
