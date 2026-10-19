# Context Mode 会话恢复发行对齐

根协调任务为 `codex-workflow-optimization/.trellis/tasks/10-19-context-mode-session-boundary`，用户已批准 revision 1。本仓库基线为 `a6868b1752c703e590571d42d9f9de477648a660`、0.2.15；目标为 0.2.16，固定经源码、真实宿主及精确 tarball 验收的 `@pennixrv/context-mode@1.0.169-pennix.6`。

本任务只修改 catalog 固定版本、工具发行版本和维护记录。既有 bootstrap/release owner 已具备完整校验、安装切换和成功后旧版清除，不添加第二套清理路径。存量恢复快照由 Context Mode 原生 `recovery` 的只读预览与摘要匹配应用处理；Lifecycle 不读取或修改组件数据库。不改变 NAS 服务边界、配置、秘密、保留策略和其他组件版本。

执行顺序为 Context Mode 发布并核对 registry → 本工具全套测试、Bash 语法、可复现发行包 → main/tag/CI GitHub Release → 当前 WSL 原生 bootstrap 升级、plan/apply/status/check。根正式子模块必须推进到同一个远端 main 提交，源码版本、实际发行 catalog 和安装包一并验收。

Context Mode `1.0.169-pennix.6` 已由源码 CI、OpenClaw E2E、发布制品真实宿主验证和 npm publish 全部通过；registry 元数据与实际下载包待本地确认后，继续发布 Lifecycle `0.2.16`。

Context Mode 的 .3 标签因 GitHub runner 的沙箱 loopback 限制在真实宿主验收失败，未发布 npm；.4 的发布验证仍因恢复线程入口的旧沙箱参数失败，.5 又确认 GitHub runner 无法创建 bwrap 用户命名空间，均未发布。源 owner 随后让本地验证保持原生只读，并仅在 GitHub 的临时 fixture 使用原生 `dangerFullAccess`，`.6` 已完整通过并作为 Lifecycle 固定版本；本工具不使用失败标签。
