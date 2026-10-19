# Skill 路径加载来源与传播

根任务：`codex-workflow-optimization/.trellis/tasks/10-19-skill-path-resolution-recurrence`，已展示方案 revision 1。用户明确批准“按照方案约定的完整流程完整推进到停止点完成”；本工作项仅落实其中已列明的本组件目标，不沿用其他旧任务批准。

修改范围：在 `templates/AGENTS.md.install` 的现有 Skill 条目明确从当前宿主目录声明取路径、仅展开根别名并保留层级；相对引用及 routing 导航保持。在现有 catalog 固定已通过的 Skills 提交，按当前发行合同发布必要的固定版本。

验证范围：模板升级与摘要保护的既有测试、完整 unittest、Bash 语法、发行资产构建和原生正式获取链。安装仅由根任务通过原生 `pennix` 更新本工具并精确修复 `codex-agents` 与 `pennix-skills`，不执行全量 apply 或手改 receipt。

基线提交：`466186b993ed193188343fe44076050b374538e3`。回退沿已验证来源及既有 owner 恢复；任何来源门禁失败、归属冲突或恢复失败均停止后续动作并保留现场。正常停止于本组件来源、固定发行物及获取链证据齐备，真实宿主验收与根侧收尾继续由根任务负责。

来源验证：完整 unittest 为 71 项通过，模板升级保护和人工审阅摘要保护均通过；模板变更已同步 catalog 的精确 SHA-256。Skills 来源固定为已推送的 `c092a018c81e52c01d671a2fe1ecb1a4c71a66fe`。本次发行版本 `v0.2.18`，仅承载上述路径来源与集合提交变化，不扩展安装目标。

固定交付：来源提交 `c97d33189d4a0e39e6a96df827ec3c26486c4bb5` 已推送；[发行 CI 39211606663](https://github.com/PennixRv/pennix-lifecycle/actions/runs/39211606663) 成功，[v0.2.18](https://github.com/PennixRv/pennix-lifecycle/releases/tag/v0.2.18) 已正式发布。通过原生 GitHub 获取的正式发行包通过摘要清单检查，且与固定来源的原生构建包逐字节一致，SHA-256 为 `9953985d44b2b452b5faf00cb69114b8e2ad2e9e3dd1d2fbe93e3d814d9c4255`；正式 bootstrap 与审阅来源也一致。

实际传播：根任务执行正式 `bootstrap.sh install --release v0.2.18 --no-input`，安装入口为 `/home/penn/.local/share/pennix-lifecycle/releases/0.2.18/pennix`；随后只执行 `codex-agents` 和 `pennix-skills` 的原生 repair。AGENTS repair 使用安装前摘要 `8e49e603a08cf1f46cad5a57c0e84714847cbf2354d9c227740242f1efa54e2d` 保护，安装后与模板同为 `3b784afda5f904f7fdf07e0247f654b6e329e479dad6b60ce696cf44f2b852e1`；Skills 对应上述固定来源。两次原生 plan 回读两个目标均为 `match / keep`，无全量 apply 或额外安装目标。

根侧安装后 4 类行为均有原生成功读取证据，当前 Paseo 的项目与主集合新会话覆盖齐备，根离线集成 9 项通过；详细证据与限制由根任务保留。正常停止点已达到，本记录提交推送后本组件停止；后续任务归档及 journal 由根仓库和 Skills 各自原生入口完成。本次补充记录不会改变 `v0.2.18` 的固定发行提交或集合来源。
