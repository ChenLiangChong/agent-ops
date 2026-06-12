"""閘道：全公司共用的 agent 身分。intake → 派工 → 逐階段執行 → 評分 → 審批 → 交付。

每個階段：
1. 組記憶注入（角色 blocks ＋ top-K facts），注入了什麼記在 run 上（歸因鏈）。
2. brain 執行（離線確定性 / claude -p 訂閱，可換）。
3. artifact 落檔（DB 只存 uri＋sha256）。
4. 有 spec → 評分；不過 → 帶缺口重跑（iterate→grade→revise，上限 max_revise）。
5. 有 approval → 開審批單（同 tx 發通知）→ 人決 → **逐欄位綁定消費** → 才跑不可逆 COMMIT。
任務收尾發 task_done 通知；途中死掉發 task_failed——都走硬接線 escalation。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import approvals, escalations, handlers, memory, queue, telemetry, workflow
from .handlers import StageCtx

# confirm(approval_id, summary) -> bool：人類審批介面（CLI input / Slack 按鈕 / demo 自動）
ConfirmFn = Callable[[int, str], bool]


@dataclass
class GatewayConfig:
    root: str                          # repo 根（workflows/ specs/ charters/ runs/ 所在）
    notify: escalations.Sender
    confirm: ConfirmFn
    brain: object
    log: Callable[[str], None] = print
    runs_override: str | None = None   # 測試把 artifact 導去 tmp 用

    @property
    def workflows_dir(self) -> str:
        return os.path.join(self.root, "workflows")

    @property
    def specs_dir(self) -> str:
        return os.path.join(self.root, "specs")

    @property
    def runs_dir(self) -> str:
        return self.runs_override or os.path.join(self.root, "runs")


class Gateway:
    def __init__(self, db: sqlite3.Connection, cfg: GatewayConfig):
        self.db, self.cfg = db, cfg
        approvals.expire_stale(db)     # 開機禮儀

    # ---- intake（adapter 驗完身分後叫這裡）----
    def intake(self, workflow_id: str, title: str, payload: dict,
               source: str, actor_id: str, external_ref: str | None = None) -> int:
        return queue.enqueue(self.db, workflow_id, title, payload, source, actor_id, external_ref)

    # ---- 派工主迴圈 ----
    def work_once(self) -> bool:
        """認領一張單並執行；回傳該單是否成功（無單可領也是 False）。"""
        task = queue.claim(self.db, "gateway")
        if task is None:
            return False
        return self.run_task(task)

    def run_task(self, task: sqlite3.Row) -> bool:
        cfg, db = self.cfg, self.db
        wf = workflow.load(cfg.workflows_dir, task["workflow"])
        payload = json.loads(task["payload"] or "{}")
        queue.set_status(db, task["id"], "running")
        cfg.log(f"\n▶ task#{task['id']} [{wf.title}]  來源={task['source']}  請求者={task['requested_by']}")

        prior: dict = {}
        for stage in wf.stages:
            ok = self._run_stage(task, stage, payload, prior)
            if not ok:
                queue.set_status(db, task["id"], "failed")
                with db:
                    escalations.enqueue_in_tx(
                        db, "task_failed", f"task#{task['id']} 在階段 {stage.id} 失敗",
                        None, actor="system", target="ops-channel")
                escalations.flush(db, cfg.notify)
                return False

        with db:
            db.execute("UPDATE tasks SET status='done', done_at=datetime('now') WHERE id=?",
                       (task["id"],))
            escalations.enqueue_in_tx(
                db, "task_done", f"task#{task['id']}「{task['title']}」完成",
                None, actor="system", target="ops-channel")
        escalations.flush(db, cfg.notify)
        cfg.log(f"✔ task#{task['id']} 完成")
        return True

    # ---- 單一階段 ----
    def _run_stage(self, task: sqlite3.Row, stage: workflow.Stage,
                   payload: dict, prior: dict) -> bool:
        cfg, db = self.cfg, self.db
        agent = db.execute("SELECT * FROM agents WHERE id=?", (stage.role,)).fetchone()
        if agent is None:
            raise KeyError(f"角色 {stage.role} 未註冊（charters/ 缺檔？）")

        injection = memory.compose_injection(
            db, stage.role, query=f"{task['title']} {stage.action} {stage.id}",
            scope_repo=payload.get("repo"))
        run_id = telemetry.start_run(
            db, task["id"], stage.role, stage.id,
            auth_mode=agent["auth_mode"], injected_memory_ids=injection.fact_ids,
            model=agent["model"])

        ctx = StageCtx(db=db, task=task, stage=stage, payload=payload,
                       injection=injection, prior=prior, run_id=run_id)
        spec = self._load_spec(stage.spec)
        result, grade = None, None
        try:
            for attempt in range(stage.max_revise + 1):
                ctx.revision = attempt
                result = cfg.brain.run_stage(ctx)
                if spec is None:
                    grade = None
                    break
                grade = handlers.GRADERS[spec["kind"]](db, spec, result)
                if grade["passed"]:
                    break
                ctx.gaps = grade["gaps"]
                cfg.log(f"  ↻ {stage.id} 評分 {grade['score']:.2f} 未過，缺口 {len(grade['gaps'])}，修訂重跑")
            else:
                raise RuntimeError(f"{stage.id} 修訂 {stage.max_revise} 輪仍未過 spec")
        except Exception as e:
            telemetry.end_run(db, run_id, status="error", error_type=type(e).__name__)
            cfg.log(f"  ✗ {stage.id} 失敗：{e}")
            return False

        artifact_id = self._save_artifact(run_id, stage, result, spec)
        if grade is not None:
            with db:
                db.execute(
                    "INSERT INTO grades (artifact_id, spec_id, score, passed, gaps, graded_by_run) "
                    "VALUES (?,?,?,?,?,?)",
                    (artifact_id, spec["_id"], grade["score"], int(grade["passed"]),
                     json.dumps(grade["gaps"], ensure_ascii=False), run_id))
        for i, (tool, ok) in enumerate(result.tool_calls):
            telemetry.log_tool_call(db, run_id, i, tool, ok)
        telemetry.end_run(
            db, run_id, status="ok",
            outcome=("pass" if grade is None or grade["passed"] else "fail"),
            input_tokens=result.tokens[0], output_tokens=result.tokens[1],
            cost_usd=0.0 if agent["auth_mode"] == "subscription" else result.tokens[1] / 1e6 * 25)

        mem_note = f"  記憶注入 {injection.fact_ids}" if injection.fact_ids else ""
        cfg.log(f"  • {stage.id} ({stage.role}) ok"
                + (f"  score={grade['score']:.2f}" if grade else "")
                + (f"  [{result.notes}]" if result.notes else "") + mem_note)

        # ---- 審批閘（不可逆動作的唯一入口）----
        if stage.approval:
            params = result.approval_params or {}
            aid = approvals.request(
                db, stage.approval,
                f"task#{task['id']} {stage.id}: {json.dumps(params, ensure_ascii=False)}",
                resume_action=stage.approval, resume_params=params, requester_run=run_id)
            escalations.flush(db, cfg.notify)
            if not cfg.confirm(aid, f"{stage.approval} {params}"):
                cfg.log(f"  ⏸ 審批 #{aid} 被拒，任務停在 review")
                queue.set_status(db, task["id"], "review")
                return False
            approvals.consume(db, aid, stage.approval, params, run_id)
            msg = handlers.COMMITS[stage.approval](db, params)
            cfg.log(f"  ✔ 審批 #{aid} 消費（參數綁定驗證過）→ {msg}")

        prior[stage.id] = result.output
        return True

    # ---- helpers ----
    def _load_spec(self, rel_path: str | None) -> dict | None:
        if not rel_path:
            return None
        path = os.path.join(self.cfg.specs_dir, rel_path)
        spec = json.load(open(path, encoding="utf-8"))
        with self.db:
            row = self.db.execute("SELECT id FROM specs WHERE path=?", (rel_path,)).fetchone()
            spec["_id"] = row["id"] if row else self.db.execute(
                "INSERT INTO specs (kind, path) VALUES (?,?)", (spec["kind"], rel_path)).lastrowid
        return spec

    def _save_artifact(self, run_id: int, stage: workflow.Stage,
                       result, spec: dict | None) -> int:
        os.makedirs(self.cfg.runs_dir, exist_ok=True)
        fname = f"run{run_id}-{stage.id}.{result.artifact_ext}"
        path = os.path.join(self.cfg.runs_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(result.artifact_content)
        sha = hashlib.sha256(result.artifact_content.encode()).hexdigest()[:16]
        with self.db:
            return self.db.execute(
                "INSERT INTO artifacts (run_id, type, uri, sha256, spec_id) VALUES (?,?,?,?,?)",
                (run_id, stage.artifact_type, os.path.join("runs", fname), sha,
                 spec["_id"] if spec else None)).lastrowid
