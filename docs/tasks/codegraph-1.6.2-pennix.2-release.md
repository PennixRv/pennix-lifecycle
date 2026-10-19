# CodeGraph 1.6.2-pennix.2 固定与 Lifecycle 发行

根任务：`codex-workflow-optimization/.trellis/tasks/10-19-managed-component-source-relocation`。本组件负责更新 CodeGraph 固定版本、Lifecycle 发行版本和本任务记录；Lifecycle 正式工具安装及 CodeGraph 本机升级由根任务使用原生 Lifecycle 入口验收。

## 修改范围

- 将 `references/catalog.json` 中 `codegraph-cli.approved_version` 从 `1.6.2-pennix.1` 更新为 `1.6.2-pennix.2`。
- 将 Lifecycle `VERSION` 从 `0.2.19` 更新为 `0.2.20`。
- 保持其他 catalog 项、升级策略、安装器、receipt、用户配置和组件状态不变。

## 基线与依据

- 开始时 Lifecycle `main`、`origin/main` 和根任务固定 gitlink 均为 `837d43ada0d41fc680e8cefa2e5d7424cfcc291d`，工作树干净；远端实时 `main` 与该提交一致。
- CodeGraph `v1.6.2-pennix.2` 已由其 owner 合并到 `main`、完成构建和回归、发布 GitHub/npm 制品并通过隔离安装验证；根任务保存完整发行证据。
- Lifecycle 远端尚无 `v0.2.20` tag 或 Release；新版本候选未占用。

## 验证、发行与停止点

- 运行完整 `python -m unittest discover -s tests -v`、`bash -n bootstrap.sh`、catalog JSON 解析、`git diff --check` 和 `scripts/build-release.sh`。
- 普通推送 Lifecycle `main` 和 `v0.2.20` tag；核验发行 CI、Release 资产、SHA-256 清单和固定源码提交一致。
- 任一源码验证、发行 CI、正式资产或摘要核验失败时，不向根任务交付可安装版本，不执行本机工具升级或 CodeGraph repair。
- Lifecycle 组件停止于 `main` 与远端一致、正式 `v0.2.20` 发行通过核验、发行任务记录已推送。根任务随后执行正式 Lifecycle 安装及 CodeGraph 精确 repair，并完成本机 package/receipt/新进程验收。

## 实际交付

- 待实施与正式发行后补记。
