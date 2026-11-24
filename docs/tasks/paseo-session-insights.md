# Paseo 会话指标接入

对应根仓任务 `11-24-paseo-session-insights`，目标分支 `main`。登记 `PennixRv/paseo-session-insights` 的固定 Git 来源，复用 `pennix paseo-plugins` 的预览、确认、版本核验和恢复流程。通过专用秘密管道向匹配的 daemon 提供 CCHP 输入。配置由目标机管理，Git 声明只用于安装来源、版本和启用状态。

验证覆盖空配置、秘密缺失、daemon 目标不匹配、专用管道输出和插件固定提交预检。先用全部合成配置，真实网关和三端验收由根任务记录。
