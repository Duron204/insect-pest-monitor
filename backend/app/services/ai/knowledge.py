# -*- coding: utf-8 -*-
"""知识库混合检索：词法精确路 + 向量语义路，两路并行、自动降级。

**为什么要两路**（这是本模块的核心设计）：
- 词法路（中文名命中 +3 / 寄主词命中 +2 / bigram 重合度）擅长
  "精确直呼"——用户说"棉铃虫"，它秒中且几乎不会错；
- 向量路（embedding 余弦）擅长"口语化语义"——"叶子上密密麻麻的
  绿色小虫"没有出现任何档案原词，但语义上就是蚜虫/粉虱；
- 任何一路挂掉（没有配 key、向量服务超时），另一路照常工作 ——
  检索的可用性不依赖外部服务的可用性。

**融合方式**：`final = lexical + vector_weight × max(cos − deadzone, 0)`。
不做复杂的 rank-fusion：两路分数量级可解释（名称直呼 +3 与 cos 0.75 × 4.0
同档），min_score 阈值语义保持不变；死区滤掉"你好"这类对任何文本
都有 0.3~0.4 底噪相似度的无关查询。

**向量缓存**：60 块语料的 embedding 写入 `data/ai_embeddings.json`
（带语料指纹），首次调用算一次、之后零成本；语料或模型变了自动重算。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..knowledge import species_list

__all__ = ["RetrievedChunk", "retrieve", "reset_cache"]

logger = logging.getLogger("insect.ai")

# 注入的向量服务：texts -> 向量列表（与 provider.embed_texts 同形）
EmbedFn = Callable[[list[str]], list[list[float]]]

_FIELD_LABELS = {
    "damage": "危害特征",
    "control": "防治建议",
    "threshold_desc": "防治阈值",
    "host_crops": "寄主作物",
}

_FIELDS = ("damage", "control", "threshold_desc", "host_crops")

_CJK_RANGE = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGE)


def _tokens(text: str) -> set[str]:
    """查询/文档共用的分词约定：CJK 双字 bigram + 拉丁单词（小写）。"""
    result: set[str] = set()
    run: list[str] = []
    text = text or ""

    def flush() -> None:
        for i in range(len(run) - 1):
            result.add(run[i] + run[i + 1])
        run.clear()

    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        else:
            flush()
    flush()
    for word in re.findall(r"[A-Za-z]{2,}", text):
        result.add(word.lower())
    return result


def _crop_terms(host_crops: str | None) -> list[str]:
    if not host_crops:
        return []
    parts = re.split(r"[、，,；;：:（）()]", host_crops)
    terms = []
    for part in parts:
        term = part.strip()
        if len(term) >= 2 and term not in {"多种", "各类", "以及", "等等"}:
            terms.append(term)
    return terms


@dataclass(frozen=True)
class RetrievedChunk:
    """一个可引用的知识块。`index` 是写进提示词的 [n] 编号。"""

    index: int
    class_id: int
    zh_name: str
    en_name: str
    field: str
    field_label: str
    text: str
    score: float


@dataclass(frozen=True)
class _Chunk:
    class_id: int
    zh_name: str
    en_name: str
    field: str
    text: str
    # 词法信号（物种级）
    species_tokens: frozenset[str]
    crops: tuple[str, ...]


_lock = threading.Lock()

# ---- 向量服务熔断 ----
# 失败一次后冷却 5 分钟：账号没 embedding 余额 / 网关限流时，
# 每次聊天都去打一次注定失败的请求（429 通常 1s 内返回），
# 积累起来既拖慢首字延迟又给对方添堵。冷却期一到自动重试，
# 服务恢复后无需重启进程。
_VECTOR_COOLDOWN_S = 300.0
_vector_failed_at: float | None = None  # None = 从未熔断（0.0 会在冷启动机器上误判）


def _vector_available() -> bool:
    return _vector_failed_at is None or (time.monotonic() - _vector_failed_at) > _VECTOR_COOLDOWN_S


@lru_cache(maxsize=1)
def _chunks() -> tuple[_Chunk, ...]:
    entries: list[_Chunk] = []
    for item in species_list():
        try:
            class_id = int(item["class_id"])
        except (KeyError, TypeError, ValueError):
            continue
        zh = str(item.get("zh_name", ""))
        en = str(item.get("en_name", ""))
        merged = " ".join([zh, en] + [str(item.get(k) or "") for k in _FIELDS])
        species_tokens = frozenset(_tokens(merged))
        crops = tuple(_crop_terms(item.get("host_crops")))
        for field in _FIELDS:
            text = str(item.get(field) or "")
            if text:
                entries.append(
                    _Chunk(
                        class_id=class_id,
                        zh_name=zh,
                        en_name=en,
                        field=field,
                        text=text,
                        species_tokens=species_tokens,
                        crops=crops,
                    )
                )
    return tuple(entries)


def reset_cache() -> None:
    """测试用：重建索引并复位向量熔断（用例间互不影响）。"""
    global _vector_failed_at
    with _lock:
        _chunks.cache_clear()
        _vector_failed_at = None


def _lexical_scores(question: str, chunks: tuple[_Chunk, ...]) -> list[float]:
    q_tokens = _tokens(question)
    q_lower = question.lower()
    scores: list[float] = []
    for chunk in chunks:
        species_boost = 0.0
        if chunk.zh_name and chunk.zh_name in question:
            species_boost += 3.0
        if chunk.en_name and chunk.en_name.lower() in q_lower:
            species_boost += 2.0
        species_boost += 2.0 * sum(1 for crop in chunk.crops if crop and crop in question)
        overlap = len(q_tokens & chunk.species_tokens)
        scores.append(species_boost + 2.0 * overlap / max(1, len(q_tokens)))
    return scores


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


# ---------------------------------------------------------------- 向量缓存
def _corpus_fingerprint(chunks: tuple[_Chunk, ...], model: str) -> str:
    hasher = hashlib.sha1()
    hasher.update(model.encode("utf-8"))
    for chunk in chunks:
        hasher.update(f"{chunk.class_id}|{chunk.field}|{chunk.text}".encode("utf-8"))
    return hasher.hexdigest()


def _chunk_key(chunk: _Chunk) -> str:
    return f"{chunk.class_id}:{chunk.field}"


def _load_vectors(
    cache_path: Path | None, fingerprint: str
) -> dict[str, list[float]] | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    vectors = data.get("vectors")
    return vectors if isinstance(vectors, dict) else None


def _save_vectors(cache_path: Path | None, fingerprint: str, vectors: dict) -> None:
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fingerprint": fingerprint, "vectors": vectors}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("向量缓存写入失败（不影响检索，下次会重算）", exc_info=True)


def retrieve(
    question: str,
    top_k: int = 4,
    min_score: float = 0.12,
    per_species_limit: int = 2,
    *,
    embed: EmbedFn | None = None,
    vector_weight: float = 4.0,
    vector_deadzone: float = 0.35,
    cache_path: Path | None = None,
    embedding_model: str = "embedding-3",
) -> list[RetrievedChunk]:
    """混合检索知识块，返回按得分降序、带 [n] 编号的引用列表。

    - `embed=None` → 纯词法（离线/降级路径，测试默认走这里）
    - 向量路径任何异常都吞掉并降级：检索可用性优先于语义增益
    """
    query = (question or "").strip()
    if not query or top_k <= 0:
        return []

    chunks = _chunks()
    lexical = _lexical_scores(query, chunks)

    vector_bonus: list[float] = [0.0] * len(chunks)
    global _vector_failed_at
    if embed is not None and _vector_available():
        try:
            vector_bonus = _vector_bonus(
                query,
                chunks,
                embed,
                vector_weight,
                vector_deadzone,
                cache_path,
                embedding_model,
            )
        except Exception as exc:  # noqa: BLE001 - 向量挂了不能拖垮检索
            _vector_failed_at = time.monotonic()
            logger.warning("向量检索失败，已降级纯词法（5 分钟内不再重试）：%s", exc)

    scored: list[tuple[float, _Chunk]] = [
        (lexical[i] + vector_bonus[i], chunks[i]) for i in range(len(chunks))
    ]
    scored = [pair for pair in scored if pair[0] >= min_score]
    scored.sort(key=lambda pair: (-pair[0], pair[1].class_id, pair[1].field))

    picked: list[RetrievedChunk] = []
    per_species: dict[int, int] = {}
    for score, chunk in scored:
        used = per_species.get(chunk.class_id, 0)
        if used >= per_species_limit:
            continue
        per_species[chunk.class_id] = used + 1
        picked.append(
            RetrievedChunk(
                index=len(picked) + 1,
                class_id=chunk.class_id,
                zh_name=chunk.zh_name,
                en_name=chunk.en_name,
                field=chunk.field,
                field_label=_FIELD_LABELS.get(chunk.field, chunk.field),
                text=chunk.text,
                score=round(score, 4),
            )
        )
        if len(picked) >= top_k:
            break
    return picked


def _vector_bonus(
    query: str,
    chunks: tuple[_Chunk, ...],
    embed: EmbedFn,
    weight: float,
    deadzone: float,
    cache_path: Path | None,
    model: str,
) -> list[float]:
    fingerprint = _corpus_fingerprint(chunks, model)
    vectors = _load_vectors(cache_path, fingerprint) or {}

    missing = [c for c in chunks if _chunk_key(c) not in vectors]
    if missing:
        fresh = embed([c.text for c in missing])
        for chunk, vector in zip(missing, fresh):
            vectors[_chunk_key(chunk)] = vector
        _save_vectors(cache_path, fingerprint, vectors)

    query_vector = embed([query])[0]
    return [
        weight * max(_cosine(query_vector, vectors.get(_chunk_key(c), [])) - deadzone, 0.0)
        for c in chunks
    ]
