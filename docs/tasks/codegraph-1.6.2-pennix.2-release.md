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

- catalog 仅将 `codegraph-cli.approved_version` 更新为 `1.6.2-pennix.2`，Lifecycle `VERSION` 更新为 `0.2.20`；未改其他组件 pin、升级逻辑、安装器、receipt 或用户配置。
- Lifecycle `main` 源码提交为 `8204604597e4d82ae79e7288f65a3b4ee871e0ff`。完整 unittest 71 项通过；`bash -n bootstrap.sh`、catalog JSON/version 解析和 `git diff --check` 通过。
- 从该提交两次独立构建的发行 tarball 均为 63,624 bytes、SHA-256 `66a6358bb435c4dea57219b574612fa426a9e8b10922149f2661a71ec58a29ac`，归档内容与版本及固定值吻合。
- `v0.2.20` 已推送且解引用到源码提交；[发行 CI 39292773320](https://github.com/PennixRv/pennix-lifecycle/actions/runs/39292773320) 的源码验证、构建和发布 job 均成功。[GitHub Release v0.2.20](https://github.com/PennixRv/pennix-lifecycle/releases/tag/v0.2.20) 正式 tarball SHA-256 为 `66a6358bb435c4dea57219b574612fa426a9e8b10922149f2661a71ec58a29ac`，下载归档的 VERSION/catalog 字段和 31 项归档清单检查通过。
- 发行后组件任务记录作为独立提交补齐，不改变已验证的 tag 源码资产；根任务继续执行正式 Lifecycle 工具安装和 CodeGraph 本机验收。
