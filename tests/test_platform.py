#!/usr/bin/env python3
"""平台測試（stdlib 自走；也相容 pytest）。每個測試用獨立 tmp DB。"""
import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentops import approvals, brains, charter, db as dbmod, distill, escalations, gateway, memory, queue, telemetry  # noqa: E402
from agentops.adapters import MockSlackApprover, MockSlackSender  # noqa: E402


def fresh_db(tmp):
    db = dbmod.open_db(os.path.join(tmp, "t.db"))
    charter.register(db, charter.load_all(os.path.join(ROOT, "charters")))
    return db


def make_gateway(db, tmp, decide=lambda aid, s: True):
    return gateway.Gateway(db, gateway.GatewayConfig(
        root=ROOT, notify=MockSlackSender(log=lambda *_: None),
        confirm=MockSlackApprover(decide, "test:boss", db, log=lambda *_: None),
        brain=brains.DeterministicBrain(), log=lambda *_: None,
        runs_override=os.path.join(tmp, "runs")))


def fixture():
    return json.load(open(os.path.join(ROOT, "fixtures", "warehouse_a.json"), encoding="utf-8"))


# ---------------------------------------------------------------- tests

def test_migrations_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.db")
        db1 = dbmod.open_db(path)
        v1 = {r["v"] for r in db1.execute("SELECT v FROM schema_migrations")}
        db1.close()
        db2 = dbmod.open_db(path)   # 重開不重套
        v2 = {r["v"] for r in db2.execute("SELECT v FROM schema_migrations")}
        assert v1 == v2 and len(v1) >= 3


def test_claim_lease():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        t1 = queue.enqueue(db, "weekly-report", "a", {}, "cli", "test:u")
        t2 = queue.enqueue(db, "weekly-report", "b", {}, "cli", "test:u")
        c1 = queue.claim(db, "gateway")
        c2 = queue.claim(db, "gateway")
        assert {c1["id"], c2["id"]} == {t1, t2}
        assert queue.claim(db, "gateway") is None          # 沒單了
        # 租約過期 → 回收重派
        with db:
            db.execute("UPDATE tasks SET claimed_at=datetime('now','-2 hours') WHERE id=?", (t1,))
        rc = queue.claim(db, "gateway")
        assert rc["id"] == t1


def test_approval_binding_and_single_consumption():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        params = {"pr": "PR#1", "sha": "abc", "count": 3}
        aid = approvals.request(db, "merge_pr", "t", "merge_pr", params, None)
        approvals.decide(db, aid, "test:boss", True)
        # 掉包參數 → 拒絕 ＋ 稽核留痕
        try:
            approvals.consume(db, aid, "merge_pr", {**params, "sha": "evil"}, None)
            assert False, "tamper 應該被拒"
        except approvals.ApprovalError:
            pass
        audit = db.execute(
            "SELECT COUNT(*) c FROM interaction_log WHERE action='approval_tamper_rejected'"
        ).fetchone()["c"]
        assert audit == 1, "tamper 稽核要在 rollback 後仍存在"
        # 型別掉包（3 vs 3.0）也要擋
        try:
            approvals.consume(db, aid, "merge_pr", {**params, "count": 3.0}, None)
            assert False
        except approvals.ApprovalError:
            pass
        # 正確消費 → 過；再消費 → 擋（單次）
        approvals.consume(db, aid, "merge_pr", params, None)
        try:
            approvals.consume(db, aid, "merge_pr", params, None)
            assert False
        except approvals.ApprovalError:
            pass


def test_approval_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        aid = approvals.request(db, "deploy", "t", "deploy", {}, None)
        with db:
            db.execute("UPDATE approvals SET expires_at=datetime('now','-1 hour') WHERE id=?", (aid,))
        assert approvals.expire_stale(db) == 1
        try:
            approvals.decide(db, aid, "test:boss", True)
            assert False, "過期單不可決"
        except approvals.ApprovalError:
            pass


def test_memory_distill_provenance_and_anti_fabrication():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        # 反捏造是 DB 約束：explicit 不附原話直接炸
        try:
            memory.add_fact(db, "x", source_type="explicit", actor="t", source_quote=None)
            assert False, "explicit 無 source_quote 應被 CHECK 擋下"
        except Exception:
            pass
        distill.record_feedback(db, "review_comment", "test:pm", "rack 高度請以 WMS 的 rack_height 為準")
        ids = distill.distill_pending(db, scope_agent="etl")
        assert len(ids) == 1
        inj = memory.compose_injection(db, "etl", "rack wms 高度")
        assert ids[0] in inj.fact_ids
        # 相似回饋 → supersede 不重複
        distill.record_feedback(db, "review_comment", "test:pm", "rack 高度一律以 WMS rack_height 欄位為準")
        ids2 = distill.distill_pending(db, scope_agent="etl")
        active = db.execute(
            "SELECT COUNT(*) c FROM memory_facts WHERE superseded_by IS NULL").fetchone()["c"]
        assert active == 1 and ids2, "相似 fact 應 supersede 而非堆疊"
        hist = db.execute("SELECT COUNT(*) c FROM memory_history").fetchone()["c"]
        assert hist >= 2   # ADD + UPDATE


def test_e2e_simready_flywheel():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        gw = make_gateway(db, tmp)
        gw.intake("simready-l1", "倉庫A CAD WMS 合成資料", fixture(), "linear", "linear:pm")
        assert gw.work_once()
        ir1 = _ir(db, tmp)
        assert ir1["rack_height_source"] == "cad_layer_default"
        # revise 迴圈真的發生過（generated 資產第一輪 fail）
        grades = db.execute("SELECT COUNT(*) c FROM grades").fetchone()["c"]
        assert grades >= 2
        distill.record_feedback(db, "review_comment", "linear:pm",
                                "rack 高度請以 WMS 的 rack_height 欄位為準，CAD 圖層預設值不可信")
        distill.distill_pending(db, scope_agent="etl")
        gw.intake("simready-l1", "倉庫A CAD WMS 合成資料", fixture(), "linear", "linear:pm")
        assert gw.work_once()
        ir2 = _ir(db, tmp)
        assert ir2["rack_height_source"] == "wms.rack_height" and ir2["rack_height"] == 7.2
        pub = db.execute("SELECT COUNT(*) c FROM scenes WHERE status='published'").fetchone()["c"]
        assert pub == 2


def test_e2e_code_change_fail_learn_pass():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        gw = make_gateway(db, tmp)
        payload = {"issue_text": "bug: csv skip first row", "area": "csv-parser"}
        gw.intake("code-change", "修 CSV 解析器 bug", payload, "linear", "linear:eng")
        assert not gw.work_once()      # 缺測試 → review 擋下
        assert db.execute("SELECT status FROM tasks WHERE id=1").fetchone()["status"] == "failed"
        distill.record_feedback(db, "review_comment", "test:boss", "修 bug 的 PR 一定要附上測試，沒有測試不收")
        distill.distill_pending(db, scope_agent="engineer")
        gw.intake("code-change", "修 CSV 解析器 bug", payload, "linear", "linear:eng")
        assert gw.work_once()
        assert db.execute("SELECT status FROM tasks WHERE id=2").fetchone()["status"] == "done"


def test_escalation_hardwired_and_no_double_send():
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        with db:
            escalations.enqueue_in_tx(db, "task_done", "x", None, "system", "ops-channel")
        sender = MockSlackSender(log=lambda *_: None)
        s1 = escalations.flush(db, sender)
        s2 = escalations.flush(db, sender)   # 第二次 flush 不重送
        assert s1["sent"] == 1 and s2["sent"] == 0 and len(sender.sent) == 1
        # 實際送出文字進稽核
        row = db.execute(
            "SELECT detail FROM interaction_log WHERE action='escalation_sent'").fetchone()
        assert "task_done" in row["detail"]


def _ir(db, tmp):
    row = db.execute(
        "SELECT a.uri FROM artifacts a JOIN runs r ON a.run_id=r.id "
        "WHERE r.stage='etl' ORDER BY a.id DESC LIMIT 1").fetchone()
    return json.load(open(os.path.join(tmp, "runs", os.path.basename(row["uri"])), encoding="utf-8"))


# ---------------------------------------------------------------- runner

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    raise SystemExit(1 if failed else 0)
