# Copilot 协作能力接入任务

关联 `codex-workflow-optimization` 根任务 collaboration-tool-capability-audit 第 11 版已批准合同 WP3；工作分支 fix/collaboration-role-host-boundaries，维护目标 main，基线 a4ee1033f0811d158d5bf246f03e94383f6767b8。

通过原生 owner 补齐 Copilot 的 CodeGraph、Context Mode MCP/Hook/三个命令 Skills，保留 Codex 现有接入及用户其他配置。新增选择不改变旧配置默认值；目标、回执与恢复按宿主和实际配置根区分。复用现有 owner 锁、受管文件、基线、漂移与恢复合同，不复制 Codex TOML，不调用 Context Mode 的全局 remove，不替代组件项目初始化。

覆盖 config/plan/status/check/update/repair/unconfigure/recover，以及隔离 COPILOT_HOME、跨宿主配置不变、异常恢复和归属验证。Context Mode 从固定来源读取原生 setup --print JSON；CodeGraph 使用原生 copilot-cli global 安装且无 --init。自定义配置目录缺共享 Skills 时仅通过 Copilot 原生目录注册维护自有新增项。

验收以根实施合同 AC1/AC3/AC4/AC5 为准：先隔离配置与失败注入，再准备精确发行候选和原生部署计划；真实 ACP 继承、同名 Skill 回退及安装后验证回根控制任务集成。原生 owner 不支持精确恢复、未知归属、策略禁用或漂移时停止相关动作，保留现场，不静默扩权。

本地完整回归 227 tests 通过、2 项环境专用测试跳过；其中 Copilot 19 项回归覆盖实际目标根、更新失败恢复更新前状态及 receipt、同一步骤身份、已验证步骤禁止失败恢复、部分撤回续接和 Hooks 禁用后撤回。首次安装撤回基线与当前操作恢复快照分别保存；恢复不撤销此前已成功的安装。写入前拒绝保留 effect=none，原生 not-started 可释放该步骤；completed 核验通过同一 owner 完成并清除恢复快照，不能遗留已接受步骤的撤销入口。全量卸载明确传递所选目标根，归属未知时阻断，撤回不要求用户重新启用 Hooks。MCP 撤回按 JSON 语义保留其他条目和现有权限；不承诺原生 owner 的 JSON 排版或空容器逐字节还原。
