# -*- coding: utf-8 -*-
"""OpenAI 兼容 /chat/completions 客户端（流式）。

**为什么不用 openai SDK / langchain**：
1. 本项目只需要"发消息、收 SSE 增量"这一件事，httpx 已在依赖里
   （测试栈），SDK 的重试/类型/多 provider 抽象全是用不上的耦合面；
2. 任何 OpenAI 兼容端点（智谱 GLM、DeepSeek、本地 Ollama）都直接可用，
   换模型 = 改 base_url + model 两个配置，不动代码；
3. 流式解析 20 行写得完、测得起（`test_ai_router.py` mock 的就是它）。

失败统一抛 `AiUnavailableError`：调用方（路由）把它翻成 SSE error 事件，
用户看到的是"模型服务暂时不可用"，而不是一个 500 堆栈。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from ...config import AIConfig

__all__ = ["AiUnavailableError", "is_configured", "stream_chat", "embed_texts"]


class AiUnavailableError(RuntimeError):
    """模型服务不可用（未配置 / 连不上 / 上游报错）。"""


def is_configured(config: AIConfig) -> bool:
    """启用且 base_url / api_key 都齐了才算配置完成。"""
    return bool(config.enabled and config.base_url and config.api_key)


def stream_chat(
    config: AIConfig,
    messages: list[dict[str, Any]],
    usage_out: dict[str, int] | None = None,
) -> Iterator[str]:
    """流式对话：逐段 yield 文本增量。

    `usage_out`：传入一个 dict，流结束时若上游回报了 token 用量
    （OpenAI 协议的末块 usage 字段），会写进 prompt/completion/total_tokens。
    这是"用量观测"的数据源 —— 生成器无法直接返回第二个值，
    用可变容器是最直白的办法。
    """
    if not is_configured(config):
        raise AiUnavailableError(
            "AI 助手未配置：需要在 config.yaml 的 ai 段填写 base_url，"
            "并通过环境变量 INSECT_AI_API_KEY 提供密钥"
        )

    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": True,
        # OpenAI 兼容协议：流式的末块附上 token 用量（GLM 支持；
        # 不认识该字段的实现会直接忽略，不会报错）
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    try:
        # Client 在 with 里逐行读：连接挂了迭代器会自然结束，
        # 不需要把整个响应读进内存
        with httpx.Client(timeout=config.timeout_s) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = response.read()[:300].decode("utf-8", errors="replace")
                    raise AiUnavailableError(
                        f"模型服务返回 HTTP {response.status_code}：{body or '（无响应体）'}"
                    )
                yield from _parse_sse(response, usage_out)
    except httpx.HTTPError as exc:
        raise AiUnavailableError(f"无法连接模型服务（{config.base_url}）：{exc}") from exc


def _parse_sse(
    response: httpx.Response, usage_out: dict[str, int] | None = None
) -> Iterator[str]:
    """解析 chat/completions 的 SSE 流，yield content 增量。

    兼容两种边角：首个 chunk 只带 role 不带 content；上游在流里嵌
    `{"error": ...}`（GLM 在鉴权失败时这么干，而不是给 HTTP 状态码）。
    """
    for line in response.iter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data:
            continue
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue  # 半行/心跳噪声：跳过比中断整个回答划算
        # 末块 usage（include_usage 开启时出现）：记到调用方给的容器里
        if usage_out is not None and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int):
                    usage_out[key] = value
        if isinstance(obj, dict) and obj.get("error"):
            message = obj["error"].get("message", str(obj["error"])) if isinstance(obj["error"], dict) else str(obj["error"])
            raise AiUnavailableError(f"模型服务返回错误：{message}")
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content



def embed_texts(config: AIConfig, texts: list[str]) -> list[list[float]]:
    """批量文本向量化（OpenAI 兼容 /embeddings，GLM embedding-3 等）。

    与 stream_chat 同一套错误语义：失败统一抛 `AiUnavailableError`，
    由 knowledge 层决定降级（向量挂了退词法，检索永远可用）。
    分批调用：一次塞几十条文本对部分网关会超请求体上限。
    """
    if not texts:
        return []
    if not is_configured(config):
        raise AiUnavailableError("AI 未配置，无法调用向量服务")

    # 智谱实测：embedding 端点是 OpenAI 风格的 /embeddings
    # （/embedding/payload 会 404，见 2026-11-08 冷烟记录）
    url = config.base_url.rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    vectors: list[list[float]] = []
    batch = max(1, config.embedding_batch_size)
    with httpx.Client(timeout=config.timeout_s) as client:
        for start in range(0, len(texts), batch):
            chunk = [t[:2000] for t in texts[start : start + batch]]
            try:
                response = client.post(
                    url, json={"model": config.embedding_model, "input": chunk}, headers=headers
                )
            except httpx.HTTPError as exc:
                raise AiUnavailableError(f"无法连接向量服务：{exc}") from exc
            if response.status_code != 200:
                body = response.text[:300]
                raise AiUnavailableError(
                    f"向量服务返回 HTTP {response.status_code}：{body}"
                )
            try:
                data = response.json().get("data", [])
                # 按 index 归位：个别网关不保证顺序
                ordered = sorted(data, key=lambda d: d.get("index", 0))
                vectors.extend([list(map(float, item["embedding"])) for item in ordered])
            except (KeyError, TypeError, ValueError) as exc:
                raise AiUnavailableError(f"向量服务返回了无法解析的数据：{exc}") from exc
    if len(vectors) != len(texts):
        raise AiUnavailableError(
            f"向量服务返回数量不符（{len(vectors)}/{len(texts)}），已放弃本次结果"
        )
    return vectors

