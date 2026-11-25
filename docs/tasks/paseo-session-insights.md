# Paseo 会话指标接入

对应根仓任务 `11-24-paseo-session-insights`，目标分支 `main`。登记 `PennixRv/paseo-session-insights` 的固定 Git 来源，复用 `pennix paseo-plugins` 的预览、确认、版本核验和恢复流程。通过专用秘密管道向匹配的 daemon 提供 CCHP 输入。配置由目标机管理，Git 声明只用于安装来源、版本和启用状态。

验证覆盖空配置、秘密缺失、daemon 目标不匹配、专用管道输出和插件固定提交预检。先用全部合成配置，真实网关和三端验收由根任务记录。

## ChatGPT Status

使用 `pennix config edit` 为会话指标插件开启 Sub2API 取数，填写控制面的 HTTPS 源地址和 OpenAI OAuth 账号 ID。使用 `pennix secrets edit` 在私有文件的 `[sub2api]` 表写入 `account_read_key`。该字段接受 Sub2API 原生受限管理员 Key，权限限定为目标账号所在分组的 `openai.quota.read`、`accounts.stats.read` 和 `accounts.identity.read`。

先升级所有读取中央秘密文件的 Lifecycle 安装，再录入新字段。`pennix secrets check` 只报告字段完整性。其他受管主机可用 `pennix secrets import-ssh --alias <可信别名> --field sub2api.account_read_key` 定向导入。
