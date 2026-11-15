# Context Mode 环境一致性与全局检查收敛

根任务：`11-15-global-lifecycle-check-green`。用户已批准根实施合同并要求推进到发布部署、双通道完整 check 全绿和归档。组件沿现有 main 实施，不新建分支或工作树。

## 修改边界

当前 Codex MCP 过滤了 user bus 环境，导致 Context Mode 内 systemctl 误报；Copilot 的 desired 每次从调用者 PATH 重新计算，导致已有接入误报更新。修改目标为本仓的 context_mode_integration.py、copilot_integrations.py、对应测试及必要文档和发行版本。复用原生 MCP env_vars、现有 extras 写入/撤回和 receipt PATH；不修改 Context Mode 核心、不增加依赖、不全量继承环境或读取父进程、不改变正常服务。

Codex 动态允许两个 user bus 名称和四个标准 XDG 用户目录名称，保留显式 CODEX_HOME 和 PATH；缺失变量不伪造。Copilot 已有接入沿用受管 PATH，首次配置及缺少 PATH 的旧回执才从原生环境捕获。真实文件漂移、模板更新、错误目标与未知归属仍保留阻断。

## 验收及交付

- 覆盖动态白名单、旧接入迁移、幂等、失败撤回及其他 owner 保留。
- 覆盖 Copilot 在不同 PATH 下的 inspect/validate/apply、旧回执、模板更新、真实漂移及失败恢复。
- 完整隔离测试、实际公开发行载荷与 CI 通过；记录固定源码、tag、资产摘要和实际安装身份。
- 根任务负责四项外部包 reconcile、原生 tool install、Codex 接入迁移、无模型请求的新 MCP 实测、双通道完整 check、recover idle 和根组合验收。完整 bootstrap 会触发系统升级，不用于本次工具安装。

实施及发布结果追加在此文件；原始输出留在临时目录，不提交配置、秘密或运行态。

## 实施与验证

两项修复复用原有入口：Codex target 增加六个名称的 env_vars；Copilot desired 优先读取已有 receipt PATH，不改变调用接口。新增四项回归覆盖变量值不进入配置、旧接入升级/失败撤回，以及 Copilot 受限/重排 PATH、模板更新、真实漂移、缺少 PATH 的旧回执迁移与故障恢复。

完整隔离 unittest 初轮运行258项，通过256项，跳过2项可选真实入口（Paseo、发行载荷）；发行载荷另在构建后验证。定向测试首次将原生 CLI 恢复时的 TOML 排序变化判为失败，现改为核对解析后的配置，Hook 字节及回执仍精确核对。发行版本选择0.3.10，公开最新版本已核对为0.3.9，没有覆盖已有 tag 或资产。
