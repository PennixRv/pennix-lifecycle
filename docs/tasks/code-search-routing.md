# 代码检索路由全局导航

根协调任务：`codex-workflow-optimization/.trellis/tasks/10-21-code-search-routing-convergence`。用户已明确批准同一四 owner 完整方案并要求持续到唯一停止点。本仓库在 main 维护 templates/AGENTS.md.install 的短导航及对应既有归属检查，基线 8098754f36b2ef3e0d9110c182fb1da21daaa1d9，工具版本 0.2.27。

仅明确加载并遵守项目外 AGENTS.md、SKILL.md 及其必读引用时直接原生全文读取，其余内容处理仍沿专责集成规则。结构检索的专责导航不再仅以“配置”触发。不维护第二份完整路由表，不修改权限、格式、其他模板或 receipt。

完整 unittest、Bash 语法、build-release 与正式包 checksum／内容通过后发行下一未占用补丁；沿固定正式 bootstrap 升级工具。根逐项更新 Context Mode、CodeGraph 和 Skills，codex-agents static 使用原生 repair；漂移仅在完整审阅后沿 expected-sha256 合同处理。不执行全量 apply，不手改安装目录、lock 或 receipt。

源码提交、正式发行、原生物化和真实宿主分别留证；失败保留首个结果和唯一续点。没有通用 rollback；使用后续正式补丁或已有 owner 恢复入口。最终完成还需要根设计中的全部宿主、组合验收和收尾条件。本文件继续记录本 owner 动态实施证据。

## 本地实施证据

候选 0.2.28 经远端 tag 不占用检查后准备；完整 unittest 97 项、bootstrap／build-release Bash 语法和 diff check 通过。正式 build-release 必须基于提交后的准确 ref，尚未执行，不以工作树模板替代正式包。

安装前 pennix plan／status 退出 0，check 退出 1：仅 Context Mode native configuration 漂移，包归属及版本匹配。只读审阅确认 MCP 等于 receipt，差异为六 Hook 使用相对命令且五个事件增加空 matcher。后续仅使用已批准的原生 repair，不修改 receipt／配置来掩盖漂移。其余已选组件和连接保持 current；functional checks 未请求。正式发行、bootstrap 升级和 codex-agents repair 仍待执行。

源码／发行提交 d06f08157509cd86b9dbf316354f53bcd410393b 已推送 main，tag v0.2.28 的官方 release 39525593084 success。原生 build-release 基于提交 HEAD 通过，下载的正式包与本地包完全一致，SHA-256 为 0ae4bda6b9bb4f222dfd345a0b458c82930739cb1deeeb2af10df2b3410da061；Release 资产摘要、checksum、VERSION 及目标模板内容核验通过。正式 bootstrap SHA-256 为 09cb43eb95a186013cc71c9710929dacadc9cb474096b2294ff74d592515cb73。后续部署和根验收仍待执行。

根已用固定正式 bootstrap 安装 Lifecycle `v0.2.28`，原生更新、`repair context-mode` 与 `repair codex-agents` 均已完成。全局 AGENTS 与本仓库安装模板摘要一致；安装后 `pennix status`、`plan`、`check` 退出码均为 0。保护快照中的 18 个非目标组件、6 个连接与 Codex config 未变；未执行全量 apply 或系统升级。真实宿主加载状态及组合门禁仍由根任务待宿主重载后验收。
