# 通用代码迭代 Skills

## auto-evolution

面向有可重复评测器的项目，组织有预算限制、多轮运行的代码或算法优化。大模型负责提出机制、修改候选和解释结果；确定性工具负责候选归档、状态记录、预算检查和评测证据管理。

适用于性能优化、算法探索和需要保留多种架构分支的迭代任务。它可以在局部优化、机制探索、研究、修复以及多步探索分支（Campaign）之间切换，并保留失败经验。切换条件、预算和完成标准由项目配置及当前任务约定确定。

### 包含的内容

| 路径 | 用途 |
| --- | --- |
| [`auto-evolution/SKILL.md`](auto-evolution/SKILL.md) | Skill 入口与迭代流程 |
| `auto-evolution/agents/openai.yaml` | Codex 展示信息 |
| `auto-evolution/scripts/evolution_cli.py` | 使用 Python 标准库的元数据管理工具 |
| `auto-evolution/scripts/test_evolution_cli.py` | 合成项目端到端回归测试 |
| `auto-evolution/references/` | 项目接口、评测、搜索、归档和分支管理规范 |
| `auto-evolution/assets/` | 项目配置、候选结果、提案、评测及分支的 JSON 模板 |

### 安装到 Codex

将 `auto-evolution/` 整个文件夹放入当前 Codex 实例的 skills 目录。默认位置示例：

```bash
git clone https://github.com/clwszhz/skills.git
mkdir -p ~/.codex/skills
cp -R skills/auto-evolution ~/.codex/skills/
```

如果设置了 `CODEX_HOME`，使用其下的 `skills/` 目录。已有同名 skill 时先检查差异并保留旧版本。重新启动或重新加载 Codex 后，可通过 `$auto-evolution` 调用。

### 接入自己的项目

建议使用 Python 3.12；发布验证使用 Python 3.12。CLI 本身无需安装第三方 Python 包，项目的编译器、运行时和评测依赖由项目提供。

项目需要提供：

- 明确的正确性标准、主指标、计时与聚合口径，以及候选和评测预算。
- 独立的可修改候选目录，以及受保护的评测器、基线和数据目录。
- 构建、快速正确性检查、开发评测的命令和机器可读结果。
- 项目适配器和隔离预检；它们对接实际源码、运行环境和评测器。

先阅读[项目配置规范](auto-evolution/references/project-contract.md)和[适配器接口](auto-evolution/references/adapter-interface.md)，再初始化配置：

```bash
python3 auto-evolution/scripts/evolution_cli.py init --help
python3 auto-evolution/scripts/evolution_cli.py init \
  --project-root /absolute/path/to/controller \
  --candidate-root /absolute/path/to/candidates \
  --state-root /absolute/path/to/state \
  --name my-project
```

初始化只生成起始配置。按实际项目填写 `.evolution/project.json`，包括适配器摘要、执行命令、隔离方式、数据集、指标和预算，然后执行：

```bash
python3 auto-evolution/scripts/evolution_cli.py validate \
  --config /absolute/path/to/controller/.evolution/project.json
```

配置模板的数值是起始值，需按任务明确约定。Campaign 的评测记账按 `max(len(evaluation.samples), 1)` 收费；如果一份结果的 12 个 sample 分别对应 12 个案例，就按 12 个样本单位记账。详见[分支管理规范](auto-evolution/references/campaign-workflow.md)。

一个调用示例：

> 使用 $auto-evolution 优化这个项目。先核对项目配置和已有运行状态，在约定预算内改进当前机制；收益进入平台期后探索不同机制。保留当前最佳候选，保持评测器、正确性和计时口径固定，完成后报告结果与证据。

### 结果与恢复

`events.sqlite3` 是权威事件记录，JSON/JSONL 归档是可重建视图。候选通过单次提案绑定工作区和上下文；结果只能消费对应提案一次。状态异常时先执行 `doctor`，需要重建视图时使用 `rebuild-state`。

开发评测、独立验证和封存测试有不同用途。若项目只有同一批公开案例的重复测试，应如实报告重复测试结果，不将它称为独立验证或泛化证据。具体规则见[评测规范](auto-evolution/references/evaluation-protocols.md)。

Skill 要求执行者持续跟进已启动的有限任务，并在恢复时核对进程和产物。它没有提供脱离 Codex 会话的常驻调度服务。CLI 管理元数据，不执行项目构建或评测命令；这些操作由执行者通过项目适配器完成。

### 检查发布副本

在仓库根目录执行：

```bash
python3 -m unittest discover -s auto-evolution/scripts -p 'test_*.py' -v
python3 auto-evolution/scripts/evolution_cli.py --help
```

回归测试使用临时目录与合成数据，检查提案、归档、预算、评测协议和状态恢复等行为，不运行实际 GPU 评测。
