# -*- coding: utf-8 -*-
"""AI 助手编排层：把检索块 + 实时数据 + 对话历史组装成一次聊天请求。

职责边界刻意压薄：**不碰网络**（provider 的事）、**不碰路由协议**
（router 的事），只做"上下文工程"——这也是 LLM 应用里真正决定
回答质量的部分：

1. 知识块带 [n] 编号写进 system，要求模型只引用存在的编号；
2. 实时数据段明确"没有数据"的情形，堵住对着空上下文编数的口子；
3. 历史截断到 `history_limit` 轮 —— 会话可以聊很久，但发出去的
   上下文必须可控（成本 + 注意力稀释）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...config import AIConfig, Settings
from ...models import User, utcnow
from ...schemas import AiChatMessage
from .context import build_data_context, collect_summary
from .knowledge import RetrievedChunk, retrieve
from .provider import embed_texts

__all__ = ["ChatPlan", "build_chat_plan"]

_MAX_QUESTION_CHARS = 500

_SYSTEM_PROMPT = """你是「病虫害智能监测系统」的植保助手，帮种植者把检测数据变成防治决策。

【回答规则】
1. 优先依据下面【知识库摘录】回答，并在结论后标注引用编号，如 [1][2]；禁止引用不存在的编号。
2. 涉及虫情数量、告警、地块时，只能使用【实时虫情数据】中的数字，禁止编造或推算新数字。
3. 知识库与实时数据都没有覆盖的问题（比如具体农药剂量），如实说明超出范围，建议咨询当地植保部门，并提示遵循农药标签与安全间隔期。
4. 实时数据显示"没有检测记录"时，必须如实说还没有数据，不要虚构趋势。
5. 用中文回答，先给结论再给依据；控制在 300 字内；列表不超过 5 条；不要输出 Markdown 表格。
6. 涉及施药的建议末尾固定提醒：具体药剂与剂量以当地植保部门指导和农药标签为准。
"""


@dataclass
class ChatPlan:
    """一次 /ai/chat 的完整执行计划。"""

    messages: list[dict[str, str]]
    citations: list[RetrievedChunk] = field(default_factory=list)
    data_context: str = ""
    truncated_history: int = 0


def build_chat_plan(
    settings: Settings,
    cfg: AIConfig,  # 已按账号解析的配置（services/ai/account.resolve_ai_config）
    db,  # noqa: ANN001 - SQLAlchemy Session，避免循环导入用鸭子类型
    user: User,
    question: str,
    history: list[AiChatMessage],
) -> ChatPlan:
    clean_question = (question or "").strip()[:_MAX_QUESTION_CHARS]

    # 检索失败不致命：少了知识上下文，助手仍可基于实时数据作答。
    # 向量路只在配置齐全时启用（retrieve 内部还有一层异常降级）
    embed = _make_embedder(cfg)
    try:
        chunks = retrieve(
            clean_question,
            top_k=cfg.context_top_k,
            min_score=cfg.knowledge_min_score,
            embed=embed,
            vector_weight=cfg.vector_weight,
            vector_deadzone=cfg.vector_deadzone,
            cache_path=settings.data_dir / "ai_embeddings.json",
            embedding_model=cfg.embedding_model,
        )
    except Exception:  # noqa: BLE001 - 知识库损坏等情况不该拖垮对话
        chunks = []

    if chunks:
        knowledge_block = "【知识库摘录】（引用时用 [编号]）\n" + "\n".join(
            f"[{c.index}] {c.zh_name} · {c.field_label}：{c.text}" for c in chunks
        )
    else:
        knowledge_block = (
            "【知识库摘录】\n（本次未检索到相关档案。若问题涉及具体害虫，"
            "请说明这是基于通用植保常识的回答，或建议用户到「虫害知识库」查询。）"
        )

    data_block = build_data_context(db, user)

    # 历史按配置截断（按消息条数计，一轮对话 = 2 条）：
    # 老消息对当前问题的影响最小，砍头保尾既省 token 又省注意力
    trimmed = list(history[-cfg.history_limit :]) if history else []
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": f"{_SYSTEM_PROMPT}\n\n{knowledge_block}\n\n{data_block}",
        }
    ]
    messages.extend({"role": m.role, "content": m.content[:2000]} for m in trimmed)
    messages.append({"role": "user", "content": clean_question})

    return ChatPlan(
        messages=messages,
        citations=chunks,
        data_context=data_block,
        truncated_history=max(0, len(history) - len(trimmed)),
    )



def _make_embedder(cfg):
    """把配置变成一个可注入 retrieve 的向量函数；未配置时返回 None。"""
    if not (cfg.enabled and cfg.base_url and cfg.api_key and cfg.vector_search):
        return None

    def embed(texts: list[str]) -> list[list[float]]:
        return embed_texts(cfg, texts)

    return embed

# ---------------------------------------------------------------- 诊断报告
_REPORT_SYSTEM_PROMPT = """你是「病虫害智能监测系统」的资深植保专家，为农场管理者撰写虫情诊断报告。

【报告结构】（严格使用下面的二级标题）
## 一、虫情概述
用实时数据说明本期总体情况；没有数据必须如实写"本期无检测记录"。
## 二、风险研判
结合重点虫种的发生规律与当前数量，判断风险等级（低/中/高）并给理由。
## 三、防治建议
按优先级列 3-5 条可执行措施；引用知识块标注 [n]；涉及药剂只给方向不给剂量。
## 四、数据附注
列出生成报告依据的数据口径与告警，方便核对。

【规则】
1. 所有数量只能来自【实时虫情数据】，禁止编造或推算。
2. 防治知识依据【知识库摘录】，结论标 [n] 引用。
3. 全文 500 字以内，条目不超过 6 条，不用表格。
4. 末尾固定一句：具体药剂与剂量以当地植保部门指导和农药标签为准。
"""


@dataclass
class ReportPlan:
    """一次诊断报告生成的执行计划。"""

    messages: list[dict[str, str]]
    range_token: str
    data_context: str
    title: str


def build_report_plan(
    settings: Settings,
    cfg: AIConfig,  # 已按账号解析的配置
    db,  # noqa: ANN001 - SQLAlchemy Session
    user: User,
    range_token: str,
) -> ReportPlan:
    """组装诊断报告：实时数据 + 重点虫种知识 → 结构化报告提示词。"""
    days = 30 if range_token == "30d" else 7
    summary = collect_summary(db, user, days)

    # 重点虫种的知识检索：用"类别名 + 防治"构造查询，
    # 而不是把整个知识库塞进去 —— 报告只关心本期实际发生的虫
    focus_names = [name for name, _count in summary.top_classes[:3]]
    chunks: list[RetrievedChunk] = []
    if focus_names:
        try:
            chunks = retrieve(
                " ".join(focus_names) + " 防治 发生规律",
                top_k=cfg.context_top_k,
                min_score=cfg.knowledge_min_score,
            )
        except Exception:  # noqa: BLE001 - 检索失败不阻塞报告
            chunks = []

    if chunks:
        knowledge_block = "【知识库摘录】（引用时用 [编号]）\n" + "\n".join(
            f"[{c.index}] {c.zh_name} · {c.field_label}：{c.text}" for c in chunks
        )
    else:
        knowledge_block = "【知识库摘录】\n（本期未检索到重点虫种档案，风险研判请基于通用植保常识并明确说明。）"

    data_block = build_data_context(db, user, days)
    title = f"虫情诊断报告（近 {days} 天 · {utcnow().strftime('%Y-%m-%d')}）"

    messages = [
        {
            "role": "system",
            "content": f"{_REPORT_SYSTEM_PROMPT}\n\n{knowledge_block}\n\n{data_block}",
        },
        {"role": "user", "content": f"请生成近 {days} 天的虫情诊断报告。"},
    ]
    return ReportPlan(messages=messages, range_token=range_token, data_context=data_block, title=title)
