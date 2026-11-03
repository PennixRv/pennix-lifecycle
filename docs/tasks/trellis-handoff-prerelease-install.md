# Trellis 交接修复的精确版本安装

## 范围与依据

根协调任务为 `codex-workflow-optimization/.trellis/tasks/10-31-new-fork-workflow-lifecycle`。
2026-11-03 用户选择 Trellis `0.7.1-*` 预发布包，并明确选择原生指定版本入口，保留
`latest` 默认通道。本仓库修改落在既有 `main`，发行版本为 `0.2.31`，不创建分支或 worktree。

## 签名及合同

`pennix update <component> [--version <exact-npm-version>]` 仅为 npm 组件增加显式版本选择。
省略参数时保持登记通道；精确版本仍从登记 registry 下载并核验包名、版本及 SHA-512，
由既有 owner 安装、后置核验、事务和 receipt 固定 SHA-256，不直接调用另一套安装器。
恢复未完成事务时可省略参数继续原目标，显式版本必须与已确认目标一致。

## 校验与错误

| 条件 | 结果 |
| --- | --- |
| 精确 npm 版本，包括预发布及 build 标识 | 按该版本解析，保持默认通道 |
| tag、范围、缺失版本段或非 npm 组件 | 解析候选前拒绝 |
| registry 返回其他版本或 tarball 完整性不符 | 拒绝候选，不写锁或 receipt |
| 恢复事务时显式版本不同 | 保留事务并拒绝切换目标 |
| 所有权、旧锁、配置或事务状态不匹配 | 保留既有拒绝规则 |

## 案例与验证

- 正常：`pennix update trellis-cli --version 0.7.1-beta.0` 展示精确候选后沿既有流程替换。
- 基线：`pennix update trellis-cli` 仍解析 `latest`；普通 `apply` 仍按锁执行。
- 拒绝：`--version beta`、`--version '^0.7.1'` 或给非 npm 组件指定版本。
- `tests/test_lifecycle_state.py` 验证 CLI 参数、精确候选、默认行为及恢复目标不可更改；
  `tests/test_candidates.py` 验证预发布 registry 选择、锁摘要和完整性失败。

错误做法是手改安装目录、catalog、组件锁或 receipt 来选择版本；正确做法是显式调用
上述原生入口，保留同一次候选确认与后置核验。真实发行及安装结果由根协调任务记录，
本文件不将静态通过等同于现场验收。

源码验证：`python3 -m unittest discover -s pennix-lifecycle/tests -p 'test_*.py'`
完成 125 项测试且全部通过，Bash 语法和 `git diff --check` 通过。CodeGraph 核对
`cmd_update`、`resolve_npm` 和 `_check_target` 的现有边界；候选解析器、安装 owner、
事务与锁格式保持原生合同，不增加第二套版本或回执状态。
