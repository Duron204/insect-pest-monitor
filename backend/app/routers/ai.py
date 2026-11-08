# -*- coding: utf-8 -*-
"""AI 助手接口：状态、流式对话（含会话持久化与用量记账）、用量汇总、诊断报告。

**聊天协议**（POST /api/ai/chat，`text/event-stream`）：
    data: {"type":"meta","conversation_id":N,"title":"…"}   # 最先下发
    data: {"type":"delta","text":"…"}
    data: {"type":"done","citations":[…],"elapsed_ms":N,"usage":{…}}
    data: {"type":"error","message":"…"}                    # 流中途失败（HTTP 仍 200）

**持久化时机**：用户消息在调模型**之前**落库（哪怕模型失败，这一问也不丢）；
助手消息在流结束**之后**落库（带 token/耗时 —— 失败的半截回答不记账，
用量观测的语义是"完成的调用"）。流生成器里的写库用 `session_factory`
新开会话，而不是复用 Depends 的那个 —— 依赖的生命周期在响应开始后
由 Starlette 管理，流式响应里复用它是在赌框架行为，新开一个才是确定性的。

**没配 key 时的行为**：503 + 配置指引（JSON，不是 SSE）——
这是"功能未启用"，发生在流开始之前，用状态码最诚实。
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db, get_settings
from ..models import AiConversation, AiMessage, AiReport, User
from ..schemas import (
    AiChatIn,
    AiCitationOut,
    AiConversationOut,
    AiMessageOut,
    AiReportOut,
    AiReportSummaryOut,
    AiStatusOut,
    AiUsageDayOut,
    AiUsageSummaryOut,
)
from ..services.ai.assistant import build_chat_plan, build_report_plan
from ..services.ai.provider import AiUnavailableError, is_configured, stream_chat
from ..services.ai.report_export import report_to_docx

router = APIRouter(prefix="/ai", tags=["ai"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Nginx 反代时禁用缓冲，否则"流式"会变成"攒完一次性吐"
    "X-Accel-Buffering": "no",
}


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _conversation_or_404(db: Session, conversation_id: int, user: User) -> AiConversation:
    conv = db.get(AiConversation, conversation_id)
    # 会话是账号私有的：别人的会话按"不存在"处理（不确认存在性）
    if conv is None or conv.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return conv


# ---------------------------------------------------------------- 状态
@router.get("/status", response_model=AiStatusOut)
def ai_status(
    settings: Settings = Depends(get_settings),
    _: User = Depends(get_current_user),
) -> AiStatusOut:
    cfg = settings.ai
    return AiStatusOut(
        enabled=cfg.enabled,
        configured=is_configured(cfg),
        model=cfg.model,
    )


# ---------------------------------------------------------------- 对话
@router.post("/chat")
def ai_chat(
    body: AiChatIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    cfg = settings.ai
    if not cfg.enabled:
        raise _not_enabled("AI 助手未启用：请在 config.yaml 的 ai 段把 enabled 设为 true")
    if not is_configured(cfg):
        raise _not_enabled(
            "AI 助手未配置完整：请在 config.yaml 的 ai 段填写 base_url，"
            "并用环境变量 INSECT_AI_API_KEY 提供密钥"
        )

    # ---- 会话解析：追问 or 新建（首问截断当标题） ----
    if body.conversation_id is None:
        conversation = AiConversation(
            owner_id=user.id, title=body.question.strip()[:128]
        )
        db.add(conversation)
        db.flush()  # 拿 id，不必先 commit
    else:
        conversation = _conversation_or_404(db, body.conversation_id, user)

    # 用户消息先落库：模型失败的这一问也不该消失
    clean_question = body.question.strip()[:500]
    db.add(AiMessage(conversation_id=conversation.id, role="user", content=clean_question))
    db.commit()

    plan = build_chat_plan(settings, db, user, body.question, body.history)
    conversation_id = conversation.id
    conversation_title = conversation.title
    # 流里的写库用工厂新开会话（理由见模块 docstring）
    session_factory = request.app.state.session_factory

    def generate():
        started = time.monotonic()
        usage: dict[str, int] = {}
        full_text: list[str] = []
        try:
            for delta in stream_chat(cfg, plan.messages, usage_out=usage):
                if delta:
                    full_text.append(delta)
                    yield _event({"type": "delta", "text": delta})
        except AiUnavailableError as exc:
            yield _event({"type": "error", "message": str(exc)})
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        answer = "".join(full_text)

        # 助手消息 + 用量记账（会话不存在时静默丢弃，不给调用方添堵）
        session = session_factory()
        try:
            conv = session.get(AiConversation, conversation_id)
            if conv is not None and conv.owner_id == user.id and answer:
                citations_payload = (
                    [
                        AiCitationOut(
                            index=c.index,
                            class_id=c.class_id,
                            zh_name=c.zh_name,
                            en_name=c.en_name,
                            field=c.field,
                            field_label=c.field_label,
                            text=c.text,
                            score=c.score,
                        ).model_dump()
                        for c in plan.citations
                    ]
                    if plan.citations
                    else None
                )
                session.add(
                    AiMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=answer,
                        citations_json=(
                            json.dumps(citations_payload, ensure_ascii=False)
                            if citations_payload
                            else None
                        ),
                        elapsed_ms=elapsed_ms,
                        prompt_tokens=usage.get("prompt_tokens"),
                        completion_tokens=usage.get("completion_tokens"),
                        total_tokens=usage.get("total_tokens"),
                        model=cfg.model,
                    )
                )
                conv.updated_at = conv.created_at  # 触发 onupdate 刷新
                session.add(conv)
                session.commit()
        finally:
            session.close()

        yield _event(
            {
                "type": "done",
                "citations": [
                    AiCitationOut(
                        index=c.index,
                        class_id=c.class_id,
                        zh_name=c.zh_name,
                        en_name=c.en_name,
                        field=c.field,
                        field_label=c.field_label,
                        text=c.text,
                        score=c.score,
                    ).model_dump()
                    for c in plan.citations
                ],
                "elapsed_ms": elapsed_ms,
                "usage": usage,
                "conversation_id": conversation_id,
            }
        )

    # meta 事件先于流下发：前端拿到会话 id 后即可刷新列表/定位
    def generate_with_meta():
        yield _event({"type": "meta", "conversation_id": conversation_id, "title": conversation_title})
        yield from generate()

    return StreamingResponse(
        generate_with_meta(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/conversations", response_model=list[AiConversationOut])
def list_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = 50,
) -> list[AiConversationOut]:
    conversations = db.scalars(
        select(AiConversation)
        .where(AiConversation.owner_id == user.id)
        .order_by(AiConversation.updated_at.desc())
        .limit(max(1, min(limit, 200)))
    ).all()
    counts: dict[int, int] = dict(
        db.query(AiMessage.conversation_id, func.count(AiMessage.id))
        .group_by(AiMessage.conversation_id)
        .all()
    )
    return [
        AiConversationOut(
            id=c.id,
            title=c.title,
            message_count=counts.get(c.id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}", response_model=list[AiMessageOut])
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AiMessageOut]:
    conversation = _conversation_or_404(db, conversation_id, user)
    messages = db.scalars(
        select(AiMessage)
        .where(AiMessage.conversation_id == conversation.id)
        .order_by(AiMessage.id)
    ).all()
    result: list[AiMessageOut] = []
    for message in messages:
        citations: list[AiCitationOut] = []
        if message.citations_json:
            try:
                payload = json.loads(message.citations_json)
                citations = [AiCitationOut.model_validate(item) for item in payload]
            except (ValueError, TypeError):
                citations = []  # 历史数据损坏时降级为无引用，不让一条坏 JSON 拖垮列表
        result.append(
            AiMessageOut(
                id=message.id,
                role=message.role,  # type: ignore[arg-type]
                content=message.content,
                citations=citations,
                elapsed_ms=message.elapsed_ms,
                total_tokens=message.total_tokens,
                created_at=message.created_at,
            )
        )
    return result


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    conversation = _conversation_or_404(db, conversation_id, user)
    # 先删消息再删会话：SQLite 的 FK CASCADE 依赖 PRAGMA，显式删最稳
    db.query(AiMessage).filter(AiMessage.conversation_id == conversation.id).delete()
    db.delete(conversation)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- 用量观测
@router.get("/usage/summary", response_model=AiUsageSummaryOut)
def usage_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    days: int = 14,
) -> AiUsageSummaryOut:
    """按账号聚合 LLM 用量：消息级 token/耗时记账 → 成本审计。

    只统计 assistant 消息（user 消息不产生调用）；calls 即完成次数。
    """
    since = _now() - timedelta(days=max(1, min(days, 90)))
    query = (
        select(AiMessage)
        .join(AiConversation, AiMessage.conversation_id == AiConversation.id)
        .where(
            AiConversation.owner_id == user.id,
            AiMessage.role == "assistant",
            AiMessage.created_at >= since,
        )
    )
    messages = list(db.scalars(query))
    day_buckets: dict[str, AiUsageDayOut] = {}
    for message in messages:
        day = message.created_at.strftime("%Y-%m-%d")
        bucket = day_buckets.setdefault(
            day, AiUsageDayOut(day=day, calls=0, total_tokens=0)
        )
        bucket.calls += 1
        bucket.total_tokens += message.total_tokens or 0
    elapsed_values = [m.elapsed_ms for m in messages if m.elapsed_ms is not None]
    return AiUsageSummaryOut(
        calls=len(messages),
        conversations=db.scalar(
            select(func.count(AiConversation.id)).where(AiConversation.owner_id == user.id)
        )
        or 0,
        reports=db.scalar(
            select(func.count(AiReport.id)).where(AiReport.owner_id == user.id)
        )
        or 0,
        prompt_tokens=sum(m.prompt_tokens or 0 for m in messages),
        completion_tokens=sum(m.completion_tokens or 0 for m in messages),
        total_tokens=sum(m.total_tokens or 0 for m in messages),
        avg_elapsed_ms=(
            int(sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0
        ),
        by_day=sorted(day_buckets.values(), key=lambda d: d.day),
    )


# ---------------------------------------------------------------- 诊断报告
@router.post("/report")
def generate_report(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    range_token: str = "7d",
) -> StreamingResponse:
    """流式生成诊断报告；完成后落库为快照（见 AiReport 的说明）。"""
    cfg = settings.ai
    if not cfg.enabled or not is_configured(cfg):
        raise _not_enabled("AI 助手未配置，无法生成报告（先完成 ai 段配置）")
    token = range_token if range_token in ("7d", "30d") else "7d"
    plan = build_report_plan(settings, db, user, token)
    session_factory = request.app.state.session_factory
    owner_id = user.id

    def generate():
        started = time.monotonic()
        usage: dict[str, int] = {}
        full_text: list[str] = []
        try:
            for delta in stream_chat(cfg, plan.messages, usage_out=usage):
                if delta:
                    full_text.append(delta)
                    yield _event({"type": "delta", "text": delta})
        except AiUnavailableError as exc:
            yield _event({"type": "error", "message": str(exc)})
            return

        content = "".join(full_text)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        report_id: int | None = None
        if content:
            session = session_factory()
            try:
                report = AiReport(
                    owner_id=owner_id,
                    range_token=token,
                    title=plan.title,
                    content=content,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    elapsed_ms=elapsed_ms,
                )
                session.add(report)
                session.commit()
                session.refresh(report)
                report_id = report.id
            finally:
                session.close()
        yield _event(
            {
                "type": "done",
                "report_id": report_id,
                "title": plan.title,
                "elapsed_ms": elapsed_ms,
                "usage": usage,
            }
        )

    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/report", response_model=list[AiReportSummaryOut])
def list_reports(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = 20,
) -> list[AiReportSummaryOut]:
    rows = db.scalars(
        select(AiReport)
        .where(AiReport.owner_id == user.id)
        .order_by(AiReport.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [AiReportSummaryOut.model_validate(row) for row in rows]


@router.get("/report/{report_id}", response_model=AiReportOut)
def get_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiReportOut:
    report = _report_or_404(db, report_id, user)
    return AiReportOut.model_validate(report)


@router.get("/report/{report_id}/export")
def export_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """诊断报告导出为 Word。"""
    report = _report_or_404(db, report_id, user)
    data = report_to_docx(
        report.title,
        report.content,
        meta=f"范围 近{30 if report.range_token == '30d' else 7}天 · "
        f"生成于 {report.created_at.strftime('%Y-%m-%d %H:%M')} UTC · "
        f"耗时 {report.elapsed_ms or 0}ms",
    )
    filename = f"虫情诊断报告-{report.created_at.strftime('%Y%m%d')}.docx"
    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(filename)}"
        },
    )


# ---------------------------------------------------------------- 内部
def _report_or_404(db: Session, report_id: int, user: User) -> AiReport:
    report = db.get(AiReport, report_id)
    if report is None or report.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    return report


def _not_enabled(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _now():
    from ..models import utcnow

    return utcnow()


def _urlquote(value: str) -> str:
    from urllib.parse import quote

    return quote(value)
