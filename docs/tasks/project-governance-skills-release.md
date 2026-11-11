# 项目治理集合固定与 Lifecycle 发行

本组件继承根任务 `trellis-project-governance-spec-orchestration` 的实施及发布部署授权，只承担必要集合 pin 和原生发行，不另建实施方案。根任务保存完整合同、用户授权、组合验收及现场部署结果。

本轮将 `pennix-skills` 来源固定为 `9563702a9b71c6f27c98f65d4a94847275938057`，对应项目 AGENTS 治理与初始化编排；Lifecycle 版本从 `0.2.52` 更新为 `0.2.53`。其他组件 pin、配置、秘密、安装器和事务协议保持现有合同。开始时源码基线为 `f651b5a6d2fb6258d1e76aaaf1af4f9ec4dedc3c`，公开最新发行为 `v0.2.52`。

验证沿既有完整 unittest、原生 `scripts/build-release.sh` 与隔离发行包测试执行；公开发行由 `.github/workflows/release.yml` 生成并发布。根侧核验公开 archive 校验文件、固定来源及内容后，通过该包自身的 `pennix tool install --json` 更新工具，再由原生 Lifecycle 更新选定集合。不得手工替换安装树、回执或锁。

完整隔离 unittest 执行 228 项，结果通过，其中两项按原生条件跳过：未选择固定 Paseo 原生测试，以及尚未提供 archive 的发行包测试。`bash -n bootstrap.sh` 和 `git diff --check` 通过。Skills 固定提交已推送，其原生集合 CI 成功；发行包测试将在从固定提交构建 archive 后单独执行。本文件更新时发布及部署尚未完成，实际结果随后补记；版本候选或局部检查不代表正式交付。

## 实际交付

- 固定源码提交 `c3fea2a` 经原生构建后，独立 payload 测试通过，实际覆盖隔离安装、失败恢复及重装。
- `v0.2.53` tag 已正常推送，[原生发行 CI 42984876969](https://github.com/PennixRv/pennix-lifecycle/actions/runs/42984876969) 成功；[公共发行](https://github.com/PennixRv/pennix-lifecycle/releases/tag/v0.2.53) archive 的 SHA-256 为 `2e6157e54127aaf1883480756220b826ef0e0989424bbd477197e3225fa90ca9`，与公共校验文件一致。39 项归档成员安全，VERSION、集合 pin 及解压后的 tar 与固定源码原生构建一致。
- 根侧通过已核验公共包自身的 `pennix tool install --json` 安装 `0.2.53`，再由 `pennix update pennix-skills --json` 核对精确候选并物化固定集合。选定治理及路由文件与源码一致；config check 通过，recover 为 idle，既有五份配置指纹保持。未重跑系统升级、修改安装树或手写锁和回执。
- 本组件交付完成；根任务继续 Trellis 发布、项目资产更新与组合验收。本记录后续提交不改变 `v0.2.53` 固定的发行内容。
