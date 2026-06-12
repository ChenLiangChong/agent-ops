"""傳輸 adapter：先模擬、真接口留著。

形狀照 sme-ai-kit line-channel 的管線：**驗簽 → 正規化 → 落 DB → 身分分流 → 通知**。
mock 版把「驗簽」換成模擬旗標、「推播」換成印 terminal；
換真 Linear（AgentSessionEvent webhook）/ Slack（app）時只換 I/O 兩端，管線不動。
教訓也搬：通知 meta 一律字串（int/None 會讓下游整包靜默丟棄）。
"""
from __future__ import annotations

from . import identity


class MockLinearAdapter:
    """模擬 Linear「把 issue 指派給 agent」：AgentSessionEvent 的形狀。"""

    def __init__(self, gateway, log=print):
        self.gw, self.log = gateway, log

    def receive_issue(self, *, issue_key: str, title: str, workflow: str,
                      payload: dict, user_id: str, signature_ok: bool = True) -> int | None:
        actor = identity.webhook_actor("linear", user_id, signature_ok)
        if not actor.verified:
            self.log(f"[Linear] {issue_key} 簽章驗證失敗 → fail-closed 丟棄")
            return None
        self.log(f"[Linear] {issue_key}「{title}」由 {actor.id} 指派 → 進佇列")
        return self.gw.intake(workflow, title, payload, source="linear",
                              actor_id=actor.id, external_ref=issue_key)


class MockSlackSender:
    """escalations 的投遞端。真版＝Slack chat.postMessage；mock＝印出來。"""

    def __init__(self, log=print):
        self.log = log
        self.sent: list[tuple[str, str]] = []

    def __call__(self, target: str, text: str) -> bool:
        self.sent.append((target, text))
        self.log(f"[Slack→#{target}] {text}")
        return True


class MockSlackApprover:
    """人類審批介面（mock）：真版＝Slack 互動訊息按鈕／PR merge。"""

    def __init__(self, decide_fn, approver: str, db, log=print):
        from . import approvals as _ap
        self._approvals = _ap
        self.decide_fn, self.approver, self.db, self.log = decide_fn, approver, db, log

    def __call__(self, approval_id: int, summary: str) -> bool:
        approve = self.decide_fn(approval_id, summary)
        self.log(f"[Slack 審批] #{approval_id} {summary} → {'✅ 核准' if approve else '❌ 駁回'}"
                 f"（{self.approver}）")
        self._approvals.decide(self.db, approval_id, self.approver, approve)
        return approve
