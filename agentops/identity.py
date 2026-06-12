"""verified-actor：身分走「傳輸層簽章 → 系統 → DB」，永遠不經過模型。

設計出處 sme-ai-kit：floored session 完全忽略 agent 自報的 actor；
這裡同理——requested_by / feedback author 只能由 adapter 在驗簽後給，
模型側 API 一律不收 actor 參數。config 注入失敗 fail-closed 成最低權限。
"""
from __future__ import annotations

import getpass
from dataclasses import dataclass

UNVERIFIED = "__unverified__"


@dataclass(frozen=True)
class Actor:
    id: str            # 'linear:user_abc' / 'slack:U123' / 'github:charlie' / 'cli:charlie'
    display: str
    verified: bool

    @property
    def safe_id(self) -> str:
        return self.id if self.verified else UNVERIFIED


def cli_actor() -> Actor:
    """本機 CLI：OS 使用者就是身分（單機開發信任邊界）。"""
    user = getpass.getuser()
    return Actor(id=f"cli:{user}", display=user, verified=True)


def webhook_actor(provider: str, user_id: str | None, signature_ok: bool) -> Actor:
    """adapter 在 HMAC/簽章驗證後呼叫。驗證失敗 → fail-closed。"""
    if not signature_ok or not user_id:
        return Actor(id=UNVERIFIED, display="unverified", verified=False)
    return Actor(id=f"{provider}:{user_id}", display=user_id, verified=True)
