"""CLI：python -m agentops <cmd>。開發者的第四個傳輸入口（另三個：Linear/Slack/GitHub）。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import approvals, brains, charter, db as dbmod, distill, escalations, gateway, identity, memory, telemetry, workflow
from .adapters import MockSlackSender


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _open(args):
    return dbmod.open_db(args.db)


def _gateway(database, args, confirm=None):
    sender = MockSlackSender()
    return gateway.Gateway(database, gateway.GatewayConfig(
        root=_root(), notify=sender, brain=brains.DeterministicBrain(),
        confirm=confirm or _interactive_confirm(database)))


def _interactive_confirm(database):
    def confirm(approval_id: int, summary: str) -> bool:
        ans = input(f"審批 #{approval_id} {summary} — 核准？ [y/N] ").strip().lower()
        ok = ans == "y"
        approvals.decide(database, approval_id, identity.cli_actor().id, ok)
        return ok
    return confirm


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentops", description="公司代理人平台")
    p.add_argument("--db", default=os.path.join(_root(), "db", "agentops.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建 DB＋註冊角色")
    sub.add_parser("demo", help="跑完整離線 demo（六幕）")
    d = sub.add_parser("dispatch", help="派一張工單")
    d.add_argument("workflow")
    d.add_argument("-t", "--title", required=True)
    d.add_argument("-p", "--payload", help="payload JSON 檔路徑")
    sub.add_parser("work", help="認領並執行佇列中的工單（互動審批）")
    sub.add_parser("tasks", help="列工單")
    sub.add_parser("memory", help="列組織記憶")
    sub.add_parser("flush", help="投遞 pending 通知")
    sub.add_parser("summary", help="遙測摘要")
    args = p.parse_args(argv)

    if args.cmd == "demo":
        import run_demo  # noqa: F401 — repo 根的 demo 腳本
        return run_demo.main()

    database = _open(args)

    if args.cmd == "init":
        charter.register(database, charter.load_all(os.path.join(_root(), "charters")))
        memory.set_block(database, None, "org-conventions",
                         "繁體中文台灣用語；交付一律走 PR；不可逆動作必過審批；數字一律來自遙測。")
        print("✔ DB 就緒，角色已註冊：",
              [r["id"] for r in database.execute("SELECT id FROM agents ORDER BY id")])
    elif args.cmd == "dispatch":
        charter.register(database, charter.load_all(os.path.join(_root(), "charters")))
        payload = json.load(open(args.payload, encoding="utf-8")) if args.payload else {}
        gw = _gateway(database, args)
        tid = gw.intake(args.workflow, args.title, payload, "cli", identity.cli_actor().id)
        print(f"✔ task#{tid} 進佇列（workflow={args.workflow}）")
    elif args.cmd == "work":
        gw = _gateway(database, args)
        if not gw.work_once():
            print("佇列無工單")
    elif args.cmd == "tasks":
        for r in database.execute("SELECT id, workflow, status, title, requested_by FROM tasks ORDER BY id"):
            print(f"#{r['id']:<4} {r['status']:<9} [{r['workflow']}] {r['title']}  ←{r['requested_by']}")
    elif args.cmd == "memory":
        for r in database.execute(
                "SELECT id, category, fact, source_type, scope_agent FROM memory_facts "
                "WHERE superseded_by IS NULL ORDER BY id"):
            print(f"#{r['id']} [{r['category']}|{r['source_type']}|{r['scope_agent'] or 'org'}] {r['fact']}")
    elif args.cmd == "flush":
        print(escalations.flush(database, MockSlackSender()))
    elif args.cmd == "summary":
        print(json.dumps(telemetry.summary(database), ensure_ascii=False, indent=2))
    return 0
