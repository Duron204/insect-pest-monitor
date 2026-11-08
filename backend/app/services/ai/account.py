# -*- coding: utf-8 -*-
"""每账号 AI 配置（BYOK）的解析与工具。

全局 `config.yaml` 的 ai 段只提供**默认参数**（温度、上限、检索配置等）；
接入凭证（base_url / api_key / model）完全来自当前账号在网页里保存的
`ai_user_config` 行 —— 站长部署时不需要（也不应该）配自己的 key。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import AIConfig, Settings
from ...models import AiUserConfig, User

__all__ = ["get_user_config", "mask_key", "resolve_ai_config"]


def get_user_config(db: Session, user: User) -> AiUserConfig | None:
    return db.scalars(
        select(AiUserConfig).where(AiUserConfig.user_id == user.id)
    ).first()


def mask_key(key: str) -> str:
    """脱码预览：只露前 2 后 4；短密钥全打码。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:2]}****{key[-4:]}"


def resolve_ai_config(settings: Settings, db: Session, user: User) -> AIConfig | None:
    """把账号行 + 全局默认参数合成一次调用用的 AIConfig。

    返回 None = 该账号从未配置（聊天/报告接口据此 503）。
    注意**不做全局回退**：站长在 yaml 里放 key 会让所有账号共用，
    违背"每个账号不互通"的隔离约定。
    """
    row = get_user_config(db, user)
    if row is None or not row.base_url or not row.api_key:
        return None
    defaults = settings.ai
    return AIConfig(
        enabled=True,
        base_url=row.base_url.strip(),
        api_key=row.api_key.strip(),
        model=(row.model or defaults.model).strip(),
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        timeout_s=defaults.timeout_s,
        history_limit=defaults.history_limit,
        context_top_k=defaults.context_top_k,
        knowledge_min_score=defaults.knowledge_min_score,
        vector_search=defaults.vector_search,
        embedding_model=defaults.embedding_model,
        vector_weight=defaults.vector_weight,
        embedding_batch_size=defaults.embedding_batch_size,
        vector_deadzone=defaults.vector_deadzone,
    )
