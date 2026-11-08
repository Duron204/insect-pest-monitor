# -*- coding: utf-8 -*-
"""混合检索（向量 + 词法）的融合与降级行为。

用可控的假向量（2 维手写）测语义路：不 mock 真模型，测的是
**融合公式、死区、缓存与降级逻辑**本身。
"""
from __future__ import annotations

import pytest

from app.services.ai.knowledge import retrieve, reset_cache


@pytest.fixture(autouse=True)
def _fresh_index():
    reset_cache()
    yield
    reset_cache()


def _fake_embed(text_vectors: dict[str, list[float]], default: list[float]):
    """文本 -> 向量的假服务：命中表里的用表，其余用 default。"""
    calls: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [text_vectors.get(t, default) for t in texts]

    embed.calls = calls  # type: ignore[attr-defined]
    return embed


def test_semantic_match_beats_lexical_noise():
    """纯语义问法（无词法重叠）靠向量路命中正确档案。"""
    # "叶子背面白色小飞虫" 词法上几乎不命中任何档案（白粉虱档案里没有这句），
    # 向量路让它命中 白粉虱.damage
    target = None
    from app.services.ai.knowledge import _chunks

    for c in _chunks():
        if c.zh_name == "白粉虱" and c.field == "damage":
            target = c.text
            break
    assert target is not None

    embed = _fake_embed(
        {target: [1.0, 0.0], "叶子背面有很多白色小飞虫怎么办": [1.0, 0.0]},
        default=[0.0, 1.0],
    )
    hits = retrieve(
        "叶子背面有很多白色小飞虫怎么办",
        top_k=4,
        min_score=1.0,  # 抬高阈值：纯词法噪声过不了线，必须靠向量分
        embed=embed,
        vector_weight=4.0,
        vector_deadzone=0.35,
    )
    assert hits, "语义匹配未生效"
    assert hits[0].zh_name == "白粉虱"
    assert hits[0].score >= 1.0  # 向量贡献 = 4 × (1.0 − 0.35) = 2.6


def test_deadzone_filters_unrelated_noise():
    """底噪相似度（cos < deadzone）不给向量分：无关问题检不出块。"""
    # 所有余弦 = 0.4：过死区但很弱？不——0.4 > 0.35 会给 4×0.05=0.2 分。
    # 用 cos = 0.2 明确在死区内，验证完全不加分。
    embed = _fake_embed({}, default=[1.0, 0.0])

    def always_low(texts):  # 查询向量与语料夹角大：cos ≈ 0.2
        return [[1.0, 0.0] if t == "你好" else [1.0, 4.9] for t in texts]

    hits = retrieve("你好", top_k=4, min_score=0.12, embed=always_low)
    assert hits == [], "死区内的相似度不应产生任何命中"


def test_vector_failure_falls_back_to_lexical():
    """向量服务抛异常 → 降级纯词法，检索结果照常返回。"""

    def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")

    hits = retrieve("棉铃虫怎么防治", top_k=3, embed=broken)
    assert hits and hits[0].zh_name == "棉铃虫"


def test_embed_none_keeps_lexical_only():
    hits = retrieve("菜螟怎么防治", top_k=3, embed=None)
    assert hits and hits[0].zh_name == "菜螟"


def test_embedding_cache_hit(tmp_path):
    """语料向量只算一次：第二次检索不再调用 embed（缓存命中）。"""
    from app.services.ai.knowledge import _chunks

    texts = {c.text: [1.0, 0.0] for c in _chunks()}
    embed = _fake_embed(texts, default=[0.0, 1.0])
    cache = tmp_path / "ai_embeddings.json"

    retrieve("棉铃虫", top_k=3, embed=embed, cache_path=cache)
    corpus_calls = len(embed.calls)  # 第一次：语料批 + 查询

    retrieve("蚜虫", top_k=3, embed=embed, cache_path=cache)
    after = len(embed.calls)
    # 第二次只应有 1 次调用（查询向量），语料来自缓存
    assert after == corpus_calls + 1
    assert cache.exists()


def test_cache_invalidated_when_model_changes(tmp_path):
    """换 embedding 模型 → 指纹变 → 缓存作废重算。"""
    from app.services.ai.knowledge import _chunks

    texts = {c.text: [1.0, 0.0] for c in _chunks()}
    embed = _fake_embed(texts, default=[0.0, 1.0])
    cache = tmp_path / "ai_embeddings.json"

    retrieve("棉铃虫", top_k=2, embed=embed, cache_path=cache, embedding_model="model-a")
    calls_a = len(embed.calls)
    retrieve("棉铃虫", top_k=2, embed=embed, cache_path=cache, embedding_model="model-b")
    assert len(embed.calls) > calls_a  # 语料重算了

def test_circuit_breaker_skips_vector_after_failure():
    """熔断：一次失败后，冷却期内即使 embed 可用也不再调用。"""
    texts: dict[str, list[float]] = {}
    from app.services.ai.knowledge import _chunks

    for c in _chunks():
        texts[c.text] = [1.0, 0.0]
    embed = _fake_embed(texts, default=[0.0, 1.0])
    calls_before_broken = 0

    def broken(texts_: list[str]) -> list[list[float]]:
        raise RuntimeError("boom")

    # 1) 失败一次 → 触发熔断
    retrieve("棉铃虫", top_k=2, embed=broken)
    calls = len(embed.calls)

    # 2) 冷却期内换可用的 embed —— 也不会被调用（词法照常出结果）
    hits = retrieve("菜螟怎么防治", top_k=2, embed=embed)
    assert hits and hits[0].zh_name == "菜螟"
    assert len(embed.calls) == calls, "熔断期内不应再调用向量服务"

    # 3) reset_cache（测试里模拟冷却结束）后恢复
    reset_cache()
    hits = retrieve("菜螟怎么防治", top_k=2, embed=embed)
    assert hits and hits[0].zh_name == "菜螟"
    assert len(embed.calls) > calls
