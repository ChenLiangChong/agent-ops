"""大腦：Planner 協定 ＋ 兩個實作。

- DeterministicBrain：離線、零依賴、可重現——demo 與測試用，也是模型掛掉時的退路。
- ClaudeCliBrain：生產路——走 **Claude 訂閱**（`claude -p`，不用 API key、不按 token 計費）。
  charter（角色鐵律）＋ 記憶注入 ＋ 階段輸入組 prompt，要求 JSON 輸出。
  這正是 OpenClaw 重用 Claude CLI 的同一條路；orchestrator 分不出兩顆腦的差別。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Protocol

from . import handlers
from .handlers import StageCtx, StageResult


class Brain(Protocol):
    def run_stage(self, ctx: StageCtx) -> StageResult: ...


class DeterministicBrain:
    def run_stage(self, ctx: StageCtx) -> StageResult:
        action = ctx.stage.action
        if action not in handlers.HANDLERS:
            raise KeyError(f"沒有 handler：{action}（用 ClaudeCliBrain 或補 handler）")
        return handlers.HANDLERS[action](ctx)


class ClaudeCliBrain:
    """每個階段 shell 一次 `claude -p`（訂閱）。需要 `claude login` 過的環境。"""

    def __init__(self, binary: str = "claude", model: str | None = None, timeout: int = 300):
        if shutil.which(binary) is None:
            raise RuntimeError(f"`{binary}` 不在 PATH——裝 Claude Code 並 `claude login`（訂閱路）")
        self.binary, self.model, self.timeout = binary, model, timeout

    def run_stage(self, ctx: StageCtx) -> StageResult:
        charter_body = open(
            ctx.db.execute("SELECT charter_path FROM agents WHERE id=?",
                           (ctx.stage.role,)).fetchone()["charter_path"],
            encoding="utf-8",
        ).read()
        prompt = (
            f"{charter_body}\n\n## 組織記憶（依此調整行為）\n{ctx.injection.text or '（無）'}\n\n"
            f"## 任務\n{ctx.task['title']}\n## 階段\n{ctx.stage.id}（{ctx.stage.action}）\n"
            f"## 輸入 payload\n{json.dumps(ctx.payload, ensure_ascii=False)}\n"
            f"## 前序階段輸出\n{json.dumps(ctx.prior, ensure_ascii=False, default=str)}\n"
            + (f"## 上輪評分缺口（修訂第 {ctx.revision} 輪）\n{json.dumps(ctx.gaps, ensure_ascii=False)}\n"
               if ctx.revision else "")
            + '\n只回 JSON：{"output": {...}, "artifact_content": "...", "notes": "..."}'
        )
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p 失敗：{proc.stderr.strip()[:300]}")
        envelope = json.loads(proc.stdout)
        data = json.loads(_extract_json(envelope.get("result", proc.stdout)))
        usage = envelope.get("usage", {})
        return StageResult(
            output=data.get("output", {}),
            artifact_content=data.get("artifact_content", ""),
            notes=data.get("notes", ""),
            tokens=(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
        )


def _extract_json(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"回覆中無 JSON：{text[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON 未閉合")
