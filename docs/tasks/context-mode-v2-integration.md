# Context Mode v2 生命周期接入

根任务为 `context-mode-routing-upstream-audit`。本仓库负责 Context Mode 包、MCP、四类 Hook、原生 trust、三个命令 Skill 和用户 AGENTS 模板的配置归属。初始发行 `0.2.34` 已发布；实际旧 trust 迁移的修复另发 `0.2.35`，保留已发布版本和 tag 不变。用户已批准根任务及其同范围实施合同。

Context Mode 固定发行是 `@pennixrv/context-mode@2.1.5-pennix.1`，来自上游 v2.1.5 与必要 Pennix 改动的普通 merge。安装不执行 npm 生命周期脚本。适配器只执行已安装入口的 `setup --print codex --json`，对输出大小、字段、路径、命令和 Hook 形状进行严格核验；Lifecycle 自己执行配置写入，不执行计划中的任意 argv，也不调用完整 setup。

MCP 通过 Codex 原生 CLI 配置。Lifecycle 显式把已选 `CODEX_HOME` 写入该 MCP 的 env，保证自定义会话目录在 Codex 过滤子进程环境后仍然一致；没有恢复旧 `CONTEXT_MODE_PLATFORM` 或 Pennix 数据根。Hook 与共享配置按条目合并，上游计算 trust hash，Lifecycle 只将其映射到合并后真实位置。schema 1 receipt 可以迁移到 schema 2，后者同时管理四 Hook、trust 和 `ctx-doctor`、`ctx-stats`、`ctx-upgrade` 三个命令 Skill。未知内容和漂移阻断写入；失败按精确旧值撤回，不覆盖其他 owner 的内容。

用户 AGENTS 模板不再强制所有调用经 Context Mode。工具选择遵守上游与宿主能力，按实际输出处理，允许本次调用无法等价承载时回退；权限拒绝不得绕过。独立集成 Skill 由 Skills 集合移除，Lifecycle 不创建替代别名。

验证结果：完整 148 项 unittest 通过，包含未知计划、外部文件、六 Hook 与旧 env 迁移、trust 漂移和失败恢复。实际公开 tarball 的 SHA-256 为 `8f43d2c9204958501844f126e43df84181f2a0bfa9641af5d37d7d6c030ca013`；`scripts/verify-context-mode-live.py` 使用真实 package owner、Codex app-server 和带空格及中文的隔离 HOME，完成安装、配置、四个 trusted Hook 核验、幂等、配置撤回与包卸载。未复制用户凭据。

已有安装须先用 `pennix update context-mode` 更新包，再用 `pennix repair context-mode` 收敛 native integration；update 本身不配置 Hook。Skills 和 `codex-agents` 分别通过自己的原生 update/repair 入口更新。实际用户重装、旧进程退出、v1 数据精确清理、备份清理和跨仓验收由根任务记录；本文件的隔离验证不代表这些步骤已完成。普通卸载继续保留用户数据。

实际旧安装在 schema 1 receipt 之外已有六个 Codex trust 条目，`0.2.34` 因未知归属安全拒绝迁移。`0.2.35` 仅对 receipt 精确拥有、当前 Hook 完全相符且原生 `hooks/list` 返回相同 command、matcher、key、currentHash 和 trusted 状态的条目收敛旧 trust；不复制 Codex hash 算法。旧六项 trust 与 Hook 一起撤回，新四项 trust 由 v2 计划与合并位置决定，失败恢复原值，其他 owner 保持不变。完整 151 项测试通过，新增成功迁移、原生 hash 失配拒绝及失败撤回；真实隔离 v1 安装的六个 trusted Hook 经包升级后迁移到四个 trusted Hook，并确认退役 trust 清除。新安装与卸载也再次验证通过。

第二次实际预检发现旧 SessionStart 后还有 AOE 的 trusted Hook：删除旧组后追加新组会占用 AOE 的位置键，`0.2.35` 因此安全拒绝。最终发行改为 `0.2.36`，重叠事件在原位置替换本组件组，避免移动其他 owner。退役或卸载若仍会移动其他 trusted Hook，在任何 MCP 或文件写入前阻断；不擅自改写其 trust 或扩大接管范围。新增回归覆盖后置 foreign trusted Hook 的迁移保留，以及卸载阻断时原文件不变。最终完整 152 项测试、实际用户迁移只读预检和真实新安装/卸载验证通过；原有两个已发布版本与 tag 均保留。
