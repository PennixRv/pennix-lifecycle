# Skill 路径加载来源与传播

根任务：`codex-workflow-optimization/.trellis/tasks/10-19-skill-path-resolution-recurrence`，已展示方案 revision 1。用户明确批准“按照方案约定的完整流程完整推进到停止点完成”；本工作项仅落实其中已列明的本组件目标，不沿用其他旧任务批准。

修改范围：在 `templates/AGENTS.md.install` 的现有 Skill 条目明确从当前宿主目录声明取路径、仅展开根别名并保留层级；相对引用及 routing 导航保持。在现有 catalog 固定已通过的 Skills 提交，按当前发行合同发布必要的固定版本。

验证范围：模板升级与摘要保护的既有测试、完整 unittest、Bash 语法、发行资产构建和原生正式获取链。安装仅由根任务通过原生 `pennix` 更新本工具并精确修复 `codex-agents` 与 `pennix-skills`，不执行全量 apply 或手改 receipt。

基线提交：`466186b993ed193188343fe44076050b374538e3`。回退沿已验证来源及既有 owner 恢复；任何来源门禁失败、归属冲突或恢复失败均停止后续动作并保留现场。正常停止于本组件来源、固定发行物及获取链证据齐备，真实宿主验收与根侧收尾继续由根任务负责。

来源验证：完整 unittest 为 71 项通过，模板升级保护和人工审阅摘要保护均通过；模板变更已同步 catalog 的精确 SHA-256。Skills 来源固定为已推送的 `c092a018c81e52c01d671a2fe1ecb1a4c71a66fe`。本次发行版本 `v0.2.18`，仅承载上述路径来源与集合提交变化，不扩展安装目标。
