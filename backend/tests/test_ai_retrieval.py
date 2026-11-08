# -*- coding: utf-8 -*-
"""知识库检索的黄金问题集评估（RAG 质量的离线、可复现证据）。

**为什么值得专门一个文件**：LLM 层的回答质量上限由检索决定 ——
检索错了，模型再强也是一本正经地引用错档案。而这套评估：
- 不花一分钱 API、不需要 mock embedding；
- 问题全是"用户会怎么问"的口语化说法，不是照抄档案原文；
- 断言 hit@3（前三块里出现期望物种），这是检索最朴素的正确性指标。
"""
from __future__ import annotations

import pytest

from app.services.ai.knowledge import retrieve, reset_cache

#: (口语化问题, 期望物种 zh_name)。覆盖三种问法路径：
#: 名称直呼 / 症状描述 / 寄主作物描述。
GOLDEN = [
    # 名称直呼
    ("菜螟怎么防治", "菜螟"),
    ("棉铃虫用什么药", "棉铃虫"),
    ("白粉虱在温室很严重怎么办", "白粉虱"),
    ("粉蚧的防治方法", "粉蚧"),
    ("果蝇怎么防", "果蝇"),
    ("蛴螬危害花生怎么处理", "蛴螬"),
    ("小菜蛾抗性很强怎么办", "小菜蛾"),
    ("菜粉蝶幼虫在吃甘蓝", "菜粉蝶"),
    # 症状/寄主描述（不提名字）
    ("白菜心叶被虫蛀了怎么办", "菜螟"),
    ("棉花蕾铃被蛀掉了", "棉铃虫"),
    ("芒果新梢叶片被卷起来结成网苞", "叶网蛾"),
    ("茄子果实里发现蛀虫", "茄果螟"),
    ("小麦叶子上密集的绿色小虫", "禾谷缢管蚜"),
    ("莴苣上有蚜虫怎么办", "合头菊蚜"),
    ("柑橘快熟的果子夜里被刺吸腐烂", "吸果夜蛾"),
    ("幼苗叶子被咬成缺刻孔洞", "灰象甲"),
    ("花期叶片上成群的红头黑甲虫", "芫菁"),
    ("番茄辣椒果实被钻蛀烂果", "棉铃虫"),
]

#: 明确无关的问题：不该检出任何块（低于阈值）。
UNRELATED = [
    "今天天气怎么样",
    "怎么注册账号",
    "你好",
    "这个系统是用什么语言写的",
]


@pytest.fixture(autouse=True)
def _fresh_index():
    reset_cache()
    yield
    reset_cache()


def test_golden_hit_at_3():
    """口语问题 → 期望物种应出现在检索前 3 块中。"""
    misses = []
    for question, expected in GOLDEN:
        hits = retrieve(question, top_k=3)
        names = [c.zh_name for c in hits]
        if expected not in names:
            misses.append(f"{question} → 检到 {names or '（空）'}，期望 {expected}")
    assert not misses, "hit@3 未命中：\n" + "\n".join(misses)


def test_golden_hit_rate_threshold():
    """整集 hit@1 ≥ 70%：名称直呼类应几乎全部第一，症状类允许落一名。"""
    hit1 = sum(
        1
        for question, expected in GOLDEN
        if retrieve(question, top_k=1) and retrieve(question, top_k=1)[0].zh_name == expected
    )
    assert hit1 >= int(len(GOLDEN) * 0.7), f"hit@1 仅 {hit1}/{len(GOLDEN)}"


@pytest.mark.parametrize("question", UNRELATED)
def test_unrelated_questions_return_nothing(question):
    assert retrieve(question, top_k=4) == [], f"无关问题检出了知识块：{question}"


def test_name_direct_hit_scores_highest():
    chunks = retrieve("棉铃虫", top_k=4)
    assert chunks and chunks[0].zh_name == "棉铃虫"
    assert chunks[0].score >= 3.0  # 名称命中是强信号


def test_citations_are_numbered_sequentially():
    chunks = retrieve("白菜心叶被虫蛀了怎么办", top_k=4)
    assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))


def test_per_species_limit():
    """同一物种最多占 2 块：避免单一害虫四个字段挤满 top-k。"""
    chunks = retrieve("棉铃虫怎么防治", top_k=4)
    counts: dict[str, int] = {}
    for c in chunks:
        counts[c.zh_name] = counts.get(c.zh_name, 0) + 1
    assert all(v <= 2 for v in counts.values()), counts


def test_empty_and_too_long_queries():
    assert retrieve("", top_k=4) == []
    assert retrieve("   ", top_k=4) == []
    assert retrieve("问题", top_k=0) == []
