# 既有项目 spec 治理集合发行

本组件复用根任务 `existing-project-spec-convergence` 第 2 版合同 W4 及用户“直接完整推进，包括发布部署和重装”的完整授权，仅固定新的 Pennix Skills 集合来源和发行元数据，不修改安装器、配置、事务或其他组件来源。

源码从 `234f3a53202451b2152ce5dd8bae2daba67b86bf` 在 `fix/existing-project-spec-convergence` 维护。集合固定为 `0cbf49faabe7813aa30b0002631d615acc8791c6`，版本候选 `0.2.56`。本仓库未初始化 Trellis，沿原生 `docs/tasks/` 记录，无手写任务运行状态。

验证沿既有完整隔离 unittest、`scripts/build-release.sh` 和显式 archive 的 payload 安装／恢复／重装测试。公开发布使用既有 tag CI，核验公共包与固定源码原生构建一致；然后由公共包自身 `pennix tool install` 更新工具，以 `pennix update` 更新精确 Trellis beta.18、集合及必要 adapter，按实际原生 help 和计划执行。系统部署不替代三个明确项目的原生资产更新与 spec 收敛。

本记录初始状态为待验证候选，不代表已发布或部署；实际结果写回本文件和根任务。
