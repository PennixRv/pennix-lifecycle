# Lifecycle 工作流成员全量对齐

根任务：`codex-workflow-optimization/.trellis/tasks/10-18-context-mode-routing-convergence`

## 目标与边界

将 Lifecycle 源 checkout、远端正式提交、版本 catalog、配置模型和 WSL 已选成员映射到同一已验收来源。当前已知活动快照为 19 个 catalog 组件加 `paseo-relay` 本地配置 owner；必须以根任务台账和 Lifecycle 实际发行物复核。

保留现有 `workflow.config` 选择。`config/defconfig-wsl` 是公开新主机默认配置，不复制本机的个性化选择。NAS/其他主机上的服务是外部依赖；本任务只管理本地客户端/配置，不接触远端服务、relay 登记、凭据或服务端部署。用户级 AGENTS 及模板只按精确差异和 owner receipt 核验，不做无差别改写。

## 已知基线

- Lifecycle 本次以 catalog 和 VERSION 更新后的 `main` 作为 `0.2.15` 发行提交；WSL 复核以该远端 tag 和 release asset 为准。
- 当前源码发行版本：`0.2.15`；上一版远端发行版本为 `0.2.14`。Context Mode catalog pin 为 `1.0.169-pennix.2`；`pennix-skills` catalog ref 为 `991bfe96217dc6066044bf4f82fadc20f20dc9d2`，`pennix-skills-extra` catalog ref 为 `4f5f88ea342a39c8eaa30261c41de984c17584dc`。本版继续固定 Context Mode 包自更新 Hook 后的 native receipt 收敛修复、显式 repair 的安全漂移恢复路径，并纳入两个 Skills collection 的最新远端提交及其物化成员 refs。
- 最新远端已补齐 `peon-ping`、`tavily-cli`、`uv` 定义，并调整多个 member pins 与 native ownership；这些改动必须先审查和测试后才用于发行物。

## 实施与验收

1. 审查远端 7 个提交、catalog、Kconfig、defconfig、owner adapters 和新增/修改测试；确认活动成员均有可追踪的 owner/source。
2. 在更新后的源码上运行完整单元测试、release build/asset checks、shell syntax 与 diff checks。
3. Context Mode `1.0.169-pennix.2` 和 Skills 最终正式版本确认后，只更新确有变化的 catalog pins；滚动版本须按所选官方通道核验并精确固定。验证模板摘要、receipt、原生 plan/apply 适配和 defconfig 选择不发生非预期变化。
4. 发布新 Lifecycle 时记录版本、commit、制品摘要和恢复版本；WSL 只通过 Lifecycle 原生发行升级与 `plan/apply/verify/check` 收敛。
5. 最终要求源码 checkout 与已发布远端提交一致，Lifecycle 发行版本与源码/catalog 一致，所有当前选择在最新 catalog 中可解释且 WSL receipt 精确吻合。

遇到私有 owner 不可访问、来源无法证明、用户配置漂移或 apply 出现清单外动作时停止相关写入并回写根任务台账，不以 status 的 `match/current` 替代远端证据。
