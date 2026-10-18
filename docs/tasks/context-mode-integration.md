# Context Mode 生命周期接入

根任务：`codex-workflow-optimization/.trellis/tasks/10-18-context-mode-fastctx-replacement`，完整方案已获批准。本仓库负责固定制品、Kconfig、包／MCP／六 Hook 归属、共享配置撤回、模板摘要保护和独立工具发行。

实施范围：删除 FastCtx 活动 schema、档位、安装及测试；新增唯一完整的 `PENNIX_COMPONENT_CONTEXT_MODE`，依赖 Codex CLI 和 Hooks；格式升至 4；唯一默认配置位于 `config/defconfig-wsl`。Context Mode 采用受管绝对入口和原生 `codex mcp add/remove`，六 Hook 按条目合并；不覆盖未知注册，不恢复共享配置快照，不删除用户索引或会话。

发行目标为 `v0.2.0`，Context Mode 制品和 Skills 提交须在实际发布、核验后固定。当前 WSL 切换属于根任务最后阶段，Paseo 重新加载在全部准备和切换检查之后。

源码门禁：53 项 unittest、Bash 语法、Git 差异检查通过。AGENTS 已部署字节由 receipt 摘要保护；人工审阅的漂移迁移另需完整文件 SHA-256，错误摘要不写入。release owner 先核验完整旧安装及新包，成功切换并落凭据后清除旧发行；未知、符号链接或越权内容阻断。旧结构只作为清理对象，不执行、不复制到新运行包。对应隔离回归已通过。

固定来源：Context Mode `1.0.169-pennix.1`；主集合 `9168c68ecf9e25e06f72706992629c5f0f7d5875`；extra `9a693a877fd7517c2201a5af4e65a9e317777c6c`。制品验收与当前 WSL 结果由根任务继续记录。

提交后复核发现单组件 `repair` 只处理包、首次接入遗漏 configure；已修正为包成功后配置该组件已选中的 native integration，配置失败继续保留原始错误，不回放全局组件或 relay。新增公共入口回归调用真实 native adapter；实际交付改用 `v0.2.1`，不消费 `v0.2.0`。

正式制品组合发现 extra 仓库根指令符号链接使安全提取拒绝。由 extra publisher 用 export-ignore 排除未交付链接，远端归档验证通过。最终来源改为 `c64c2b4f0aac9797a32bef2bf2b0554f67dfd00d`，catalog 修订发行 `v0.2.2`；不放宽消费者提取规则，后续只部署此修订版。
