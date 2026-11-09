# Paseo 工作树委派集成

承接根任务 `11-09-paseo-based-parallelism-integration-design` 的 W3；用户已授权当前会话完成实现、测试、发布重装与验收，不使用实施子代理。

新增独立可选 `trellis-paseo-adapter` npm owner，依赖选中的 Trellis/Paseo CLI，Node >=22。基础 Trellis 不依赖 SDK，原有 Node >=18.17 保持。适配器从固定 npm 候选、lock、归属 receipt 和现有允许脚本流程安装；不借全局私有 module 路径，不复制 daemon 认证。

共享 Codex/Trellis/adapter/CodeGraph/Paseo 替换前检查原生 Paseo 委派：未关闭的带委派标签 agent 或未知运行态阻断。idle 不代表可更新；先由 controller 保全成果、接受或取消并核销，再由原生 owner 关闭 agent。底层权限/上下文默认值不增加检查或新配置。

CodeGraph 的 MCP 项目绑定由已发布原生 `codegraph install --target codex --location local --bound-worktree <root>` 完成；Lifecycle 不写项目配置或自动索引。Skills collection 固定验证后的源码提交。实际发布、安装版本与真实行为在执行后补录，计划不等于通过。

实际升级 CodeGraph 时，原生平台包补装使首次版本探测失败，事务保留 unknown；随后原生 CLI 已就绪，但旧 receipt 尚未更新。0.2.43 为同一事务的 npm upgrade 增加受限恢复：要求旧 lock 与可信 receipt 同源同版本，重新核验事务固定归档摘要、包管理器与命令版本、归档内每个已安装文件；文件篡改、路径重定向、缺 receipt、外部安装均拒绝。不重放安装，不手改 journal，不把版本相同视为充分证据。其他交付方式和首次安装的 unknown 不扩展。
