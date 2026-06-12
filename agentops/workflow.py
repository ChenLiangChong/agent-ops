"""workflow 定義載入：workflows/*.json。

平台的核心立場：**公司流程是資料，不是程式**。
一條流程 = 有序 stages，每個 stage 指定：角色、動作、要不要對 spec 評分、要不要審批。
加一條公司流程 = 加一個 JSON 檔（再配 handler 或交給 LLM brain），平台不用改。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class Stage:
    id: str
    role: str                    # 對應 charter id
    action: str                  # handler key 或 LLM 指令意圖
    spec: str | None = None      # specs/*.json 相對路徑（None = 不評分）
    approval: str | None = None  # 審批類型（None = 不需審批）
    artifact_type: str = "report"
    max_revise: int = 2          # 評分不過時的修訂迴圈上限


@dataclass
class Workflow:
    id: str
    title: str
    stages: list[Stage] = field(default_factory=list)


def load(workflows_dir: str, workflow_id: str) -> Workflow:
    path = os.path.join(workflows_dir, f"{workflow_id}.json")
    data = json.load(open(path, encoding="utf-8"))
    return Workflow(
        id=data["id"],
        title=data["title"],
        stages=[Stage(**s) for s in data["stages"]],
    )


def list_ids(workflows_dir: str) -> list[str]:
    return sorted(
        os.path.splitext(f)[0] for f in os.listdir(workflows_dir) if f.endswith(".json")
    )
