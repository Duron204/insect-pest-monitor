# AI 助手（RAG + 实时虫情问答）· 实施方案

> 版本：2026-11-08（终稿） ｜ 状态：Phase 1 已实施并通过全量测试
> 目标：给监测系统加上 LLM 层，让"检测数据"变成"防治决策"——
> 这是把项目从「CV + 全栈」升级为「CV + LLM 应用 + 全栈」的一步。

---

## 一、产品定位

用户在田里/看板上问一句话，助手结合**三路上下文**回答：

1. **知识库 RAG**：15 类害虫的危害/寄主/防治/阈值档案（species.json，确定性检索）
2. **实时虫情**：近 7 天检测统计（去重虫数、类别分布、地块/设备维度，按账号隔离）
3. **告警**：未确认告警的等级与内容

典型问题：
- "白菜心叶被虫蛀了怎么办"（→ 知识库检索：菜螟）
- "这周虫情怎么样，要不要打药"（→ 实时统计 + 阈值知识）
- "有哪些告警没处理"（→ 告警上下文）

## 二、架构

```
浏览器 AI 助手页（SSE 流式渲染 + 引用徽标）
        │ POST /api/ai/chat
        ▼
routers/ai.py ── services/ai/assistant.py（组装上下文与提示词）
      ├─ knowledge.py   species.json → 分块 → 词法混合评分检索（纯离线、可测试）
      ├─ context.py     SQLite → 近7天统计/未确认告警/地块（owner 隔离）
      └─ provider.py    OpenAI 兼容 /chat/completions（GLM 等），SSE 解析
```

### 关键设计决策（为什么这么做）

| 决策 | 理由 |
|---|---|
| **混合检索：词法 + 向量双路** | 词法路（名称/寄主词/bigram）擅长精确直呼；向量路（GLM embedding-3 余弦）擅长口语化语义。`final = lexical + 4.0 × max(cos − 0.35, 0)`，死区滤掉"你好"式底噪相似度 |
| 向量降级与熔断 | 向量服务失败（如账号无 embedding 余额、限流）自动降级纯词法；连续失败后 5 分钟熔断，不再每次请求都打注定失败的接口 |
| embedding 本地缓存 | 60 块语料向量写入 `backend/data/ai_embeddings.json`（带语料+模型指纹），首次算一次、之后零成本；语料变更自动重算 |
| 数据上下文实时查库注入，不做工具调用 | 15 类 + 固定维度，预取全部摘要的 token 成本远低于多轮 tool-call 的延迟与失败面；同时避免执行不可控结构化输出 |
| 引用机制 [n] + citations 事件 | 检索块带编号写入提示词，要求模型引用；done 事件回传结构化引用（class_id/字段/得分），前端渲染为可点击徽标，跳转知识库卡片 |
| 优雅降级 | `ai.enabled=false` 或未配 key → 接口 503 并附配置指引；检索失败不阻塞回答，只是少了知识上下文 |
| 安全 | 复用 get_current_user；问题 ≤500 字；历史 ≤12 条；纯检索+生成，无工具执行面 |
| 流式协议 | SSE：`delta{text}` → `done{citations, elapsed_ms}` / `error{message}`；前端 fetch + ReadableStream 手工解析 |

## 三、配置（config.yaml）

```yaml
ai:
  enabled: true
  base_url: https://open.bigmodel.cn/api/paas/v4   # 任意 OpenAI 兼容端点
  api_key: ""            # 建议留空，用环境变量 INSECT_AI_API_KEY
  model: glm-4-flash
  temperature: 0.3
  max_tokens: 1200
  history_limit: 6       # 多轮对话最多带几轮
  context_top_k: 4       # 检索注入的知识块数
```

## 四、评估（面试可讲的部分）

- **检索质量**：`test_ai_retrieval.py` 18 条黄金问题（口语化问法 → 期望物种）断言 hit@3 全中；`test_ai_vector.py` 验证融合公式、死区、缓存、降级与熔断。这是不花钱、可复现的离线评估
- **接口行为**：401 / 503（未配置）/ SSE 事件序列（mock provider）全覆盖
- **真机冒烟**：配置 GLM key 后实测流式回答与引用

## 五、前端

- 新页面 `/assistant`（侧栏「AI 助手」）：
  - 聊天流（流式光标、引用徽标 → 跳知识库、快捷问题、空状态插画）
  - 未配置时显示配置指引（config.yaml + 环境变量），不是报错弹窗
- 登录页美化：加入田间实景照片卡（公共领域图源）+ 特性文案更新
- 图标/插画一律沿用现有令牌体系自绘 SVG，不引入外部图标库

## 六、图片版权

登录页照片来自 Wikimedia Commons，公共领域/CC0 协议，可商用、无需署名
（来源与许可记录在 `vue-frontend/public/images/IMAGE-CREDITS.md`）。

## 七、排期

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 后端 AI 层（含混合检索）+ 聊天页 + 登录美化 + 图片 + 测试 | ✅ 完成 |
| — 验证 | 后端 430 项测试全绿（含 26 项 AI 新测试）；前端 107 项单测 + vue-tsc + 构建通过；Playwright 实测：登录→AI 对话（GLM 流式）→引用跳转知识库，控制台零报错 | ✅ |
| Phase 2 | LLM 诊断报告生成（接现有 docx/xlsx 导出管线） | 待做 |
| Phase 3 | 对话历史持久化、评估集扩充、token 成本统计 | 待做 |
