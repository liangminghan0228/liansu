# 更新日志

本文件记录项目的所有重要变更。格式基于 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning 2.0.0](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- `eval/e2e_runner.py`：从原始对话出发的端到端评测链路（gate → 抽取 → confidence → supersede → 落盘 → 召回），含 `stub` / `real` 两种 provider 与 5 条标注用例（见 `docs/e2e-eval.md`）。
- `liansu metrics`：遥测聚合命令，输出会话、事实、索引与视图体量，供宿主插件消费。
- `liansu health` / `liansu endpoint`：服务可用性自检与 Deskflow 动态端点解析结果查看，支持 `--json` 与 CI 退出码。
- 健康自检覆盖宿主侧 MCP 信任/审批状态，避免工具未授权时误判为链路可用。

### Changed

- LLM 端点解析改为「Deskflow 实时连接优先 + `/health` 逐候选探测 + 缓存」，`base_url` 随 DHCP 漂移自动更新，无需手改配置。
- 请求禁用系统代理（`trust_env: false`），避免局域网端点被 `HTTP_PROXY` 转发导致 502。
- 按开源仓库规范整理目录：`demo.py` 移入 `examples/`，评测集归入 `eval/datasets/`；新增 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、GitHub Issue/PR 模板与 CI。
- 本地 `config.yaml` 不再入库，改由 `config.example.yaml` 提供脱敏示例；缺失本地配置时自动回退到示例配置。

### Fixed

- 真实链路端到端测试暴露并修复 5 个集成缺陷：fact id 跨批重复、模型编造日期、`"null"` 字符串、SQLite 跨线程、source 被覆盖（详见 `docs/E2E_REPORT.md`）。

## [0.1.0] - 2026-11-23

首个可用版本：T0–T16 共 17 个开发任务落地，形成「写入全自动、检索可消融、索引可重建、能力仅经 MCP 暴露」的完整闭环。

### Added

- **真源与契约**：`journal/YYYY-MM.md` append-only 事件流（fact / supersede），Fact schema 含双时态字段与 `to_yaml()` / `from_yaml()`；时效状态在重放时计算。
- **写入链路**：Stop hook 会话捕获与 zstd/jsonl 解析、LLM gate 判定、结构化抽取、confidence 门控、supersede 落盘。
- **读取链路**：BM25（jieba）、sqlite-vec 向量、networkx 双时态图三路索引，加权 RRF 融合、时效过滤与多跳 `expand`。
- **接口**：FastMCP server 暴露 `record` / `recall` / `expand` / `brief` / `rebuild` 五个工具，以及 `liansu` CLI 与 `agents.md` 宿主规则。
- **派生视图**：entities / topics / brief Markdown 视图，支持 `rebuild --verify` 全量重建与幂等校验。
- **评测**：自建 gold 集（24 题）、公开集转换说明、检索分阶段消融 runner 与指标（HitRate / Recall / MRR / 时效正确率）。
- **工程**：provider 层（OpenAI 兼容 / Ollama / 本地 embedding）、统一配置与环境变量覆盖、MIT License、README 与技术方案文档。

### Fixed

- 开源交付时补全 OpenAI-compatible 请求代理与系统字段，真实端点接入可用。

[unreleased]: https://github.com/liangminghan0228/liansu/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/liangminghan0228/liansu/releases/tag/v0.1.0
