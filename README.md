# 连溯 Liansu

本地长期记忆系统：以 Markdown vault 为唯一真源、以 MCP 为唯一接口、全自动抽取的个人记忆层。

> 空窗期个人项目 · 方案定稿于 2026-11-21 · 待落地

## 一句话

让任意 Agent 平台（WorkBuddy / Codex）能跨会话记住你的事实、偏好与决策——通过本地 MCP server，不依赖 Obsidian 客户端，无独立 UI。

## 文档

- [详细技术方案](docs/TECH_DESIGN.md) —— 主体架构、数据模型、三条主线、9 个关键选型的多方案对比与推荐、实施路线、风险
- [可执行任务拆分](docs/TASKS.md) —— 17 个 task / 5 个波次，按模块并行开发，每个 task 含输入、输出、命令与验收标准

## 核心设计

| 原则 | 说明 |
|---|---|
| 真源唯一 | `journal/` append-only Markdown 是唯一权威，索引与视图全部可 rebuild |
| 协议收敛 | 所有能力经 MCP 暴露：`record` / `recall` / `expand` / `brief` |
| 全自动写入 | 宿主 Stop hook → LLM gate → 抽取 → 门控 → supersede → append |
| 按需检索 | 规则引导 agent 主动调 `recall`，不每 session 强注入 brief |
| 时效内建 | 每条 fact 带 `valid_from`/`valid_to`，能回答「现在是否成立」 |
| 模型解耦 | provider 层统一；默认走 Windows 主机 OpenAI-compatible /v1，base_url / api_key / model 全部 config 化 |

## 当前状态

方案阶段。执行前需清三件事：
1. 实测 Stop hook 触发时能否拿到当前对话文本（`~/.codex/sessions/` 当前为空）；
2. 安装依赖（fastmcp / rank-bm25 / sqlite-vec / sentence-transformers / openai）；配置 Windows LLM 服务 `http://192.168.0.100:8080/v1`（`model: local`、`api_key: dummy`、关 thinking），接入前先 `curl /health` 确认 IP（详见 [技术方案 §5.2.1](docs/TECH_DESIGN.md)）；无需拉本地模型；
3. 从 vault 抽 20 条 fact 手搓最小评测集，跑通 Stage0 基线。

## 命名

延续「连X」传统（连枢 / 连阅 / 连砺），「溯」指追溯事实的时间演变，对应双时态设计。仓库名 `liansu`。
