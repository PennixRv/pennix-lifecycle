# Paseo 工作树委派集成

承接根任务 `11-09-paseo-based-parallelism-integration-design` 的 W3；用户已授权当前会话完成实现、测试、发布重装与验收，不使用实施子代理。

新增独立可选 `trellis-paseo-adapter` npm owner，依赖选中的 Trellis/Paseo CLI，Node >=22。基础 Trellis 不依赖 SDK，原有 Node >=18.17 保持。适配器从固定 npm 候选、lock、归属 receipt 和现有允许脚本流程安装；不借全局私有 module 路径，不复制 daemon 认证。

共享 Codex/Trellis/adapter/CodeGraph/Paseo 替换前检查原生 Paseo 委派：未关闭的带委派标签 agent 或未知运行态阻断。idle 不代表可更新；先由 controller 保全成果、接受或取消并核销，再由原生 owner 关闭 agent。底层权限/上下文默认值不增加检查或新配置。

CodeGraph 的 MCP 项目绑定由已发布原生 `codegraph install --target codex --location local --bound-worktree <root>` 完成；Lifecycle 不写项目配置或自动索引。Skills collection 固定验证后的源码提交。实际发布、安装版本与真实行为在执行后补录，计划不等于通过。

实际升级 CodeGraph 时，原生平台包补装使首次版本探测失败，事务保留 unknown；随后原生 CLI 已就绪，但旧 receipt 尚未更新。0.2.43 为同一事务的 npm upgrade 增加受限恢复：要求旧 lock 与可信 receipt 同源同版本，重新核验事务固定归档摘要、包管理器与命令版本、归档内每个已安装文件；文件篡改、路径重定向、缺 receipt、外部安装均拒绝。不重放安装，不手改 journal，不把版本相同视为充分证据。其他交付方式和首次安装的 unknown 不扩展。

0.2.44 修复恢复完成时的 journal 检查点引用：原生写锁会同步 journal 的 lock_digest，随后必须读取该检查点再标记 verified。新增使用真实隔离 lock/journal 的回归，修复前稳定复现“步骤更新前发生变化”，修复后同一调用完成并归档。实际 CodeGraph 事务已经沿 0.2.43 正式发行入口对账恢复，未重放安装；最终工具安装固定到 0.2.44。

0.2.45 修复真实运行态验证暴露的 busy guard 缺口：Paseo CLI列表是渲染投影，不输出labels，缺字段不能解释为无委派标签。原生完整标签通过可选adapter公开SDK只读inspect取得，验证协议、同一daemon身份及CLI已列出的全部ID，再阻断包括idle在内的未关闭委派。没有adapter且无法证明标签时保守阻断；只有原生当前caller自身时允许首次安装引导。新增真实CLI形状、idle委派、未知/缺失观察、错daemon和首次安装边界回归。只读真实preflight修复前允许、修复后明确阻断，未发起共享组件替换。
