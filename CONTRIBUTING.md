# 贡献指南

感谢你考虑参与连溯。任何形式的贡献（Issue、文档、代码、评测数据）都有价值。

## 环境准备

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml   # 本地配置不入库，按需修改 vault 根与 LLM 端点
```

需要 Python 3.11+。向量检索首次运行会下载 `BAAI/bge-small-zh-v1.5`（约 100MB）；离线开发可跳过相关用例。

## 开发流程

1. 先开 Issue 说明问题或方案，避免重复劳动；较大改动先在讨论中对齐设计。
2. 从 `main` 建短生命周期分支，命名建议 `feat/xxx`、`fix/xxx`、`docs/xxx`。
3. 小步提交，提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)：
   `feat: ...` / `fix: ...` / `docs: ...` / `test: ...` / `chore: ...` / `refactor: ...`。
4. 提交前跑测试；新增能力必须带单测，涉及真实链路的改动补端到端验证。

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m eval.runner --gold eval/datasets/own/gold.jsonl --stages 0,1,2,3
.venv/bin/python examples/demo.py                  # 离线全链路演示
```

## 代码约定

- 生产代码位于 `src/liansu/`，公开能力只经 MCP server 与 CLI 暴露；不要在业务层绕过协议直接依赖宿主。
- `journal/` 是唯一真源，索引与视图必须可由 `rebuild` 重建；新增写入路径时不得原地修改历史行。
- 配置不写死：模型、端点、阈值走 `config.yaml` / 环境变量，示例值放 `config.example.yaml`。
- 新增模块同步更新 README 目录说明；用户可感知的变更写进 `CHANGELOG.md` 的 `[Unreleased]`。

## Pull Request

- 描述改动动机、实现方式和验证结果，关联对应 Issue。
- 保持 PR 单一主题；无关重构请拆分。
- CI 通过后才考虑合并；维护者可能要求补充测试或文档。

## 行为准则

参与本项目即表示同意遵守 [Code of Conduct](CODE_OF_CONDUCT.md)。
