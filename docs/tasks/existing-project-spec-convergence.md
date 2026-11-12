# 既有项目 spec 治理集合发行

本组件复用根任务 `existing-project-spec-convergence` 第 2 版合同 W4 及用户“直接完整推进，包括发布部署和重装”的完整授权，仅固定新的 Pennix Skills 集合来源和发行元数据，不修改安装器、配置、事务或其他组件来源。

源码从 `234f3a53202451b2152ce5dd8bae2daba67b86bf` 在 `fix/existing-project-spec-convergence` 维护。集合固定为 `95bf6ba9731560069eaf793b6542b3279f0a9064`，包含 project-update 旧句式测试修复，版本候选 `0.2.56`。本仓库未初始化 Trellis，沿原生 `docs/tasks/` 记录，无手写任务运行状态。

验证沿既有完整隔离 unittest、`scripts/build-release.sh` 和显式 archive 的 payload 安装／恢复／重装测试。公开发布使用既有 tag CI，核验公共包与固定源码原生构建一致；然后由公共包自身 `pennix tool install` 更新工具，以 `pennix update` 更新精确 Trellis beta.18、集合及必要 adapter，按实际原生 help 和计划执行。系统部署不替代三个明确项目的原生资产更新与 spec 收敛。

## 实际结果

固定发行源码 `7a54d0550a52327fd41adfba09f1432fa8cca8f7`，tag `v0.2.56`；公开 CI [43143302221](https://github.com/PennixRv/pennix-lifecycle/actions/runs/43143302221) 成功。源码测试 228 项，226 通过、2 跳过；实际发行载荷安装、恢复和重装测试通过。

公共 archive SHA256 为 `db399ff602560f5aeaaf4d8026e029a8de96e93302a2f1a4f53ec95dc78bbcb3`，与公共校验文件及 GitHub asset digest 一致。与本地固定构建的 gzip 摘要不同，但解压 tar 完全相同，39 个条目的名称、模式、类型、正文一致。已从核验的公共载荷执行 `pennix tool install --json`，实际返回 `installed 0.2.56`，没有执行新机系统升级。

已安装工具通过原生 `update pennix-skills` 及 `repair pennix-skills` 成功更新、重装上述固定集合，原生 status 为 match。Trellis beta.18 尚无 GitHub 发布运行记录及公共 npm 包，CLI/adapter 和三个项目资产暂未更新；这属于根侧尚未完成的组合交付，不能因本工具已发布而关闭根任务。

## 发布恢复后的部署结果

前述 Trellis 阻塞已解除：同一固定发布运行 `43146526460` 在公共 npm 可见后重跑成功，固定 tag／版本不变。公共包身份与内容核验通过后，本工具串行执行 CLI、adapter 的原生 update 与 repair，均为 beta.18、status match；集合固定 `95bf6ba...` 且 match，recover idle。没有手写锁、receipt 或配置，也没有运行新机全量系统升级。根、Trellis、Skills 项目资产更新和真实 spec 迁移由原生 Trellis 与已授权治理方法完成，不归本安装器代写。

全局 `check` 另报告 aoe、codex-cli、pandoc 的系统包锁与实际版本不一致，以及 paseo-autostart 的 user-manager／自启状态不可核验；这些目标不属于本合同，没有擅自 reconcile、升级系统包或重启当前 daemon。责任仍为各原生 owner 与 Lifecycle，后续环境治理触发时处理。不能据三个本次目标 match 宣称全局健康检查通过。
