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

## 发布部署与接受结果

修复源码 `dcd7c428c894dbc20c01178b4e16d11dcbdf31c2` 已推送 main，tag `v0.3.10` 指向该提交。发布 CI `43697463103` 成功，源码门禁258项通过（2项可选入口跳过），实际发行载荷门禁1项通过。公开地址为 https://github.com/PennixRv/pennix-lifecycle/releases/tag/v0.3.10 。

公开 tarball SHA-256：`03808c10a756ff070a2eedafabcdb2e3acc264f1aaf333d95c7d07d9ec01ddea`，与公开校验文件、GitHub asset digest 和 CI 构建摘要一致。本地压缩包摘要不同，但解压后的 tar 字节完全相同，SHA-256 为 `4812fb209139e4a6c95ea85b5e23b5500a4c5c34586fa54af6174b5642d1f21f`；52个成员的内容和元数据一致。未将压缩包差异当作相同资产，公开下载载荷另跑安装/恢复/重装门禁1项通过，随后以该公开载荷执行原生 tool install。

实际安装为0.3.10，47个运行文件与公开载荷逐字节一致。原生 `repair context-mode` 对既有固定 `2.1.10-pennix.1` 完成接入迁移，保留其他 owner。原生完整 check 与新真实 Codex MCP 内的完整 check 均退出0，31个已选择组件及8个连接的完整语义状态完全相同，recover idle；四个外部包仅同步身份，版本未变。

通过真实 app-server 的原生线程和 `mcpServer/tool/call` 验证三组环境：原生、四个自定义 XDG 目录、六个变量全部未设置。ctx_execute 和 ctx_batch 均动态匹配允许变量，合成秘密不转发；缺失 user bus 时真实 systemctl 查询退出1，不伪造通过。每组四个 Hook trusted、模型请求0，验证线程原生归档，验证宿主退出。探针须先经原生无模型 shell 操作落盘 session_meta，并按接口传递实际线程元数据；未落盘线程和缺失元数据的调用被拒绝，未绕开 Context Mode 会话校验。

7项配置/秘密/Hook 保留摘要均一致，只有计划新增的 Codex env_vars 被排除后作配置语义比较；Paseo 原有 supervisor PID 305、健康和自启状态保留。当前长会话的旧 MCP 环境不强制替换；本次验收使用实际新启动、读取受管配置的 MCP。可选 Paseo 隔离真实入口未额外重跑，付费功能检查维持 not_requested。组件交付已接受，根组合验收及 Trellis 归档由根任务负责。
