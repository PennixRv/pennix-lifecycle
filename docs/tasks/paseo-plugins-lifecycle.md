# Paseo 插件生命周期接管

目标分支为 `main`，对应根仓任务 `11-23-paseo-plugins-lifecycle-management`。在 Lifecycle 中接管 Chat Polish 和 Phone notify 的固定 Git 提交、启用状态与受管设置。Paseo v0.17.1 的公开 SDK 执行插件变更，配对、凭据、daemon 和 Relay 继续由既有原生入口管理。

按 [CLI 说明](../../README.md#paseo-插件) 使用限定范围的 `pennix paseo-plugins`。复用原有组件锁、事务记录和工具发布流程。验证覆盖固定提交首装、Chat Polish 同提交注册表迁移、Phone notify 原位采用、设置漂移和启停、版本更新和降级，以及解除接管、卸载和断线续接。根仓保留发布和各端验收证据。
