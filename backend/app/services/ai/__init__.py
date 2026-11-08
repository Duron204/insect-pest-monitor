# -*- coding: utf-8 -*-
"""LLM 助手服务层。

子模块：
- knowledge : 知识库检索（species.json → 分块 → 词法混合评分，纯离线可测）
- context   : 实时虫情上下文（统计 / 告警 / 地块 / 设备，按账号隔离）
- provider  : OpenAI 兼容 /chat/completions 客户端（SSE 解析，无 SDK 依赖）
- assistant : 组装提示词与引用的编排层

设计取舍见项目根《AI助手设计方案.md》。
"""
