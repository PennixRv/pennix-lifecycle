# 用户规则与 Context Mode PATH 收敛

根任务 `codex-workflow-optimization/.trellis/tasks/in_progress/11-08-native-questioning-agents-optimization` 的完整方案已获用户接受并授权实施、提交推送、发行、安装及收尾；设计 revision 为 `4ad9ae36246d6169eac079ef4102fe99231e7684beaf7e4849a0db402d605183`。

本组件基线 main `3c67f54e0a00aa66df8024f401185cc10a817dc8`、0.2.36。只修改 AGENTS 源模板、Context Mode MCP 环境归属与幂等迁移、相关测试文档和固定 Skills 来源，发布下一未占用补丁。原生配置和安装由现有 owner 写入，不修改安装副本或手写 receipt。

行为差距是宿主同步工具被局部清单误判，以及 MCP 从父进程继承缺少用户 CLI 目录的 PATH；用户同时要求禁止异步提问。AGENTS 保留原有 26 条语义并明确异步禁令，具体方法由 Skills 承接。Context Mode 保持上游环境继承语义，只由本工具显式设置局部 PATH；既有归属值用于稳定检查，无 PATH 旧 receipt 经明确 repair 迁移。漂移、未知归属、不安全输入停止。

实施顺序：模板与 PATH 改动 → 相关和完整 unittest/发行检查 → 固定公开 Skills 提交的隔离物化和模型评估 → 固定 catalog → 新补丁构建/tag/CI/公开制品核验 → 原生安装和 repair → 新宿主/MCP/Tavily 验收及根集成。唯一完成点为完整交付和根侧验收；失败用原生事务恢复，不假定旧工具兼容新增 PATH，不覆盖公开版本。

## 发布候选

版本 0.2.38；catalog 固定公开 Skills `75952b97fa3472ba447def69391d3cdc8a08b379`，resolver 尊重声明的 ref，既有锁定目标仍优先。真实模型使用同一 collection 内容 digest `05f0ac7429628249d957d745fbf1d160a9f589da2a8e631a3eb88f1465d3577b`，后续提交只修正评估器的宿主标志计量和本仓任务证据，不改变 Skills 内容。

完整 unittest 157 项通过；迁移、较窄父 PATH 下稳定检查、包装器依赖链、拒绝坏 receipt 与事务恢复均有回归。AGENTS 源模板逐字等于已批准候选，4988 字节、SHA-256 `305cebc9697bc55c93b116bcf7bae9712230b7313d642d1d72099fe1d8d4dca7`；26 条原有规则的语义覆盖表保存在根任务 review.md。

根任务已完成 8 个真实模型回合和 4 个真实 app-server/本地模型协议检查。前后样本均未调用异步工具，基线不复现历史误报；Default 中同步 request_user_input 的 isBlocking=false 不代表异步。最终发行、原生安装、新 MCP 查询和现场收尾由根任务继续核验。

v0.2.37 标签已公开，但 CI 42444945567 在生成 Release 前失败：PATH 过滤测试假设 Node 位于 /usr/bin，GitHub runner 不满足。修正为在夹具用户 CLI 目录建立指向真实 Node 的符号链接；产品实现未变。保留失败标签，不重打、不覆盖，使用新补丁 v0.2.38。

## 实际安装与接受

v0.2.38 对应提交 `20b2503701d429cdff77c4c669792b41aa4a41d8`，CI 42445163319 success。公开 runtime SHA-256 为 `7a9126a0cf369b42f723ca5ce1bb3fae41780af986455c091a4368769b175eba`，与公开 checksum 及 GitHub asset digest 一致。公开包与本地构建的解压 tar 摘要同为 `f77a0276f42c6043cbd0b4482607f4955e7523e2dc8c341578443a073caef194`；gzip 头及压缩字节不同，未将本地压缩包冒充正式制品。经公开包原生 tool install，再以正式 bootstrap --release v0.2.38 --no-input 复验，返回 current 0.2.38；现有系统依赖均跳过，无新增依赖。

随后原生 update 安装固定 Skills；摘要保护 repair 安装 AGENTS 候选；context-mode repair 返回 no-op; native repaired，包锁与版本保持 2.1.5-pennix.1。非目标 Codex 配置解析摘要、Hooks 原始字节、CM 非环境属性及非 PATH env 全部保持。旧 no-PATH 配置迁移成功，后续 native 状态为 current。

在实际用户 CODEX_HOME 的独立真实宿主完成同步问答、全文/统计，以及一次新 MCP 中的受限 Tavily 查询，tvly-hikari、tvly 和 CCH 均可见，唯一查询退出 0。未技术隐藏异步工具，不声称永久阻止模型误用。

完整 pennix check 仍因安装前已存在的 SiYuan connection configuration-required 返回 1；前后全部连接状态相同，本任务三个目标组件分别为 match/match/current。此项非本合同目标，未重配或读取凭据。根 offline 11/11，根 fixture 8 项通过。当前组件合同满足，后续根集成及原生关闭由根任务负责。本次后续记录只改 docs，不改 v0.2.38 已发布 runtime。
