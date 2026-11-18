# CodeGraph Codex 分层路由接入

承接根任务 codegraph-context-mode-upstream-sync 已批准的第 2 版 W03—W06。Lifecycle 只接管原生安装定义及确切 Hook 信任，不写组件查询算法、不初始化项目，不扩大为系统更新。

从已核验安装入口在隔离 CODEX_HOME 执行原生 CodeGraph global install，严格验证 MCP 与 SessionStart／UserPromptSubmit 两个 handler，转换为实际受管可执行路径。旧包无 Hook 时保留 MCP-only schema 1，包更新后升级到 schema 2。实际合并定义后查询 Codex 原生 hooks/list 的身份、command、sourcePath、key、currentHash，再写入这两个确切信任并复验 trusted；不猜测 hash、不信任整份文件。

复用既有 compare-and-replace、Hook 合并、foreign trust 守卫、辅助 TOML 字段写入及外层 journal。已有字段或 trust 漂移拒绝写入；撤回保留中间空槽，避免移动外部 handler 的信任键；失败按原字节撤回本次 MCP、Hook 和 trust。原生项目信任及本树独立可用索引仍由 CodeGraph 检查，生命周期不自动更改。

5 个新增回归覆盖 schema 1 迁移、幂等、外部位置、原生 hash 核验失败、receipt 失败撤回、trust 漂移、禁用 Hook；既有 native 35 项通过。完整 334 项通过，4 项因环境条件跳过。随后收紧 receipt handler 结构及原生运行态核验，新增 5 项再次通过；最终完整与公开载荷仍须发行前验证。

真实 Codex 0.169.1 的隔离 owner 测试证明实际信任、原生预取、重复配置及撤回通过，无外部模型调用。正式部署只使用公开固定 runtime 的 scripts/install_tool.py，不运行会执行 pacman -Syu 的 bootstrap；两设备实际交付与唯一真实模型轮次由根协调记录。
