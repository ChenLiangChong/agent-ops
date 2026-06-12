#!/usr/bin/env python3
"""agent-ops 完整離線 demo（六幕）。零依賴、免 API key、免網路。

    python3 run_demo.py

演的是同一件事的六個面向：**一個公司代理人，接全公司的流程，且越用越像員工**。
每一幕結尾都有斷言——demo 同時是 smoke test。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentops import approvals, brains, charter, db as dbmod, distill, gateway, memory, telemetry  # noqa: E402
from agentops.adapters import MockLinearAdapter, MockSlackApprover, MockSlackSender  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "db", "demo.db")


def banner(text):
    print(f"\n{'═' * 64}\n{text}\n{'═' * 64}")


def main() -> int:
    # 每次重來：demo 可重現
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)

    db = dbmod.open_db(DB_PATH)
    charter.register(db, charter.load_all(os.path.join(ROOT, "charters")))
    memory.set_block(db, None, "org-conventions",
                     "繁體中文台灣用語；交付一律走 PR；不可逆動作必過審批；數字一律來自遙測。")

    slack = MockSlackSender()
    approver = MockSlackApprover(lambda aid, s: True, "slack:U_BOSS", db)
    gw = gateway.Gateway(db, gateway.GatewayConfig(
        root=ROOT, notify=slack, confirm=approver, brain=brains.DeterministicBrain()))
    linear = MockLinearAdapter(gw)
    fixture = json.load(open(os.path.join(ROOT, "fixtures", "warehouse_a.json"), encoding="utf-8"))

    # ════ 幕一：倉庫數位分身 pipeline 第一次 ════
    banner("幕一｜simready-l1 第一次：CAD＋WMS → SimReady twin → 合成資料")
    linear.receive_issue(issue_key="LIN-101", title="把 倉庫A 的 CAD 圖與 WMS 匯出變成可訓練的合成資料集",
                         workflow="simready-l1", payload=fixture, user_id="user_pm")
    assert gw.work_once()
    ir1 = _latest_ir(db)
    print(f"\n→ 第一次 IR：rack 高度 {ir1['rack_height']}（來源 {ir1['rack_height_source']}）"
          "  ← CAD 圖層預設值，埋了雷")
    assert ir1["rack_height_source"] == "cad_layer_default"
    assert db.execute("SELECT COUNT(*) c FROM scenes WHERE status='published'").fetchone()["c"] == 1

    # ════ 幕二：人類回饋 → 蒸餾進組織記憶 ════
    banner("幕二｜PM 在 PR 上留 review 意見 → 蒸餾成有出處的組織記憶")
    fb = "rack 高度請以 WMS 的 rack_height 欄位為準，CAD 圖層預設值不可信"
    distill.record_feedback(db, "review_comment", "linear:user_pm", fb)
    new_ids = distill.distill_pending(db, scope_agent="etl")
    fact = db.execute("SELECT * FROM memory_facts WHERE id=?", (new_ids[0],)).fetchone()
    print(f"→ memory_facts#{fact['id']} [{fact['category']}|{fact['source_type']}] {fact['fact']}")
    print(f"  出處（source_quote，DB 層強制）：「{fact['source_quote']}」")
    assert fact["source_type"] == "explicit" and fact["source_quote"] == fb

    # ════ 幕三：同單重派 → 行為改變（越用越像員工）════
    banner("幕三｜同一張單重派：記憶注入 → 第二次沒犯第一次的錯")
    linear.receive_issue(issue_key="LIN-102", title="把 倉庫A 的 CAD 圖與 WMS 匯出變成可訓練的合成資料集",
                         workflow="simready-l1", payload=fixture, user_id="user_pm")
    assert gw.work_once()
    ir2 = _latest_ir(db)
    print(f"\n→ 第二次 IR：rack 高度 {ir2['rack_height']}（來源 {ir2['rack_height_source']}）")
    assert ir2["rack_height_source"] == "wms.rack_height" and ir2["rack_height"] == 7.2
    run2 = db.execute(
        "SELECT injected_memory_ids FROM runs WHERE stage='etl' ORDER BY id DESC LIMIT 1").fetchone()
    assert fact["id"] in json.loads(run2["injected_memory_ids"])
    print(f"  歸因鏈：runs.injected_memory_ids = {run2['injected_memory_ids']} ✓（回饋可記功/究責到具體記憶）")

    # ════ 幕四：工程流程 fail → 學 → pass ════
    banner("幕四｜code-change：第一次卡在 review（缺測試）→ 學到規矩 → 第二次過")
    linear.receive_issue(issue_key="LIN-103", title="修 CSV 解析器跳過第一行資料的 bug",
                         workflow="code-change", payload={"issue_text": "bug: csv skip first row",
                                                          "area": "csv-parser"}, user_id="user_eng")
    assert not gw.work_once()          # review 擋下（high finding：缺測試）
    print("→ 第一次：reviewer 擋下（缺測試），task failed、Slack 已收到通知")
    distill.record_feedback(db, "review_comment", "slack:U_BOSS", "修 bug 的 PR 一定要附上測試，沒有測試不收")
    distill.distill_pending(db, scope_agent="engineer")
    linear.receive_issue(issue_key="LIN-104", title="修 CSV 解析器跳過第一行資料的 bug",
                         workflow="code-change", payload={"issue_text": "bug: csv skip first row",
                                                          "area": "csv-parser"}, user_id="user_eng")
    assert gw.work_once()
    print("→ 第二次：engineer 記得帶測試 → review 過 → merge 審批核准 ✓")

    # ════ 幕五：審批防掉包 ════
    banner("幕五｜審批安全性：核准的是「動作＋參數」，掉包直接拒絕")
    aid = approvals.request(db, "deploy", "部署 twin 服務 v3", "deploy",
                            {"version": "v3", "site": "warehouse-A"}, None)
    approvals.decide(db, aid, "slack:U_BOSS", True)
    try:
        approvals.consume(db, aid, "deploy", {"version": "v99", "site": "warehouse-A"}, None)
        raise AssertionError("掉包竟然成功——不該發生")
    except approvals.ApprovalError as e:
        print(f"→ 換參數消費被拒：{e}")
    tamper = db.execute(
        "SELECT COUNT(*) c FROM interaction_log WHERE action='approval_tamper_rejected'").fetchone()["c"]
    assert tamper >= 1
    approvals.consume(db, aid, "deploy", {"version": "v3", "site": "warehouse-A"}, None)
    try:
        approvals.consume(db, aid, "deploy", {"version": "v3", "site": "warehouse-A"}, None)
        raise AssertionError("重複消費竟然成功——不該發生")
    except approvals.ApprovalError as e:
        print(f"→ 二次消費被拒（單次消費）：{e}")

    # ════ 幕六：週報 ＋ 人改 diff（飛輪二號）════
    banner("幕六｜weekly-report：scribe 出草稿，人改的 diff 也是學習訊號")
    linear.receive_issue(issue_key="LIN-105", title="本週營運週報", workflow="weekly-report",
                         payload={}, user_id="user_pm")
    assert gw.work_once()
    distill.record_feedback(db, "edit_diff", "linear:user_pm",
                            "週報開頭要先講重點結論，再放數字明細")
    distill.distill_pending(db, scope_agent="scribe")
    print("→ 草稿產出，人改 diff 已收進回饋（下週的草稿會先講結論）")

    # ════ 收尾 ════
    banner("收尾｜遙測與稽核")
    s = telemetry.summary(db)
    facts = db.execute("SELECT COUNT(*) c FROM memory_facts WHERE superseded_by IS NULL").fetchone()["c"]
    audit = db.execute("SELECT COUNT(*) c FROM interaction_log").fetchone()["c"]
    print(f"任務 {s['tasks_done']}/{s['tasks']} 完成｜runs {s['runs']}（訂閱 {s['subscription_runs']}）｜"
          f"token in/out {s['input_tokens']:,}/{s['output_tokens']:,}｜API 成本 ${s['cost_usd']}")
    print(f"組織記憶 {facts} 條（皆可溯源）｜稽核紀錄 {audit} 筆｜Slack 通知 {len(slack.sent)} 則")
    assert s["cost_usd"] == 0.0, "全程訂閱，不該有 API 花費"
    print("\nRESULT: PASS ✓  一個代理人、三條公司流程、兩次「教一次就會」、零 API 費用")
    return 0


def _latest_ir(db):
    row = db.execute(
        "SELECT a.uri FROM artifacts a JOIN runs r ON a.run_id=r.id "
        "WHERE r.stage='etl' ORDER BY a.id DESC LIMIT 1").fetchone()
    return json.load(open(os.path.join(ROOT, row["uri"]), encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
