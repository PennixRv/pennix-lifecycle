# Codex 会话身份兼容与旧 feature 归属收敛

承接根任务 `11-14-codex-proxy-cache-compaction-investigation` 的已批准实施修复，沿用组件 main，不创建分支，不回退根任务阶段。

## 范围与实施合同

历史会话绑定 `model_provider` ID。home 已由用户恢复为 `OpenAI`，CCHP URL、私有认证、500K 和 provider V2 保持。Lifecycle 对此选择 PRESERVE；不放宽内置保留名校验，不修改会话元数据。

复用 `repair codex-config --expected-sha256`：摘要必须匹配完整当前文件，已有可信归属必须存在。只允许释放不再选择、且人工已恢复首次基线的漂移字段；仍被选择的字段或未知漂移继续拒绝。正常 apply/inspect/uninstall 的漂移保护不变，不提供任意接管或 force 入口。

停止生成已移除的 `features.remote_compaction_v2`；旧 Kconfig bool 暂保留为兼容输入，避免旧配置成为未知选项。仅在可信旧归属存在且字段通过漂移检查时移除旧 feature，不删除用户外部维护的同名字段，不改 provider 的能力声明。

修改 codex_config owner、repair 参数归属、Kconfig 提示、回归测试和发行版本。使用现有 README 的发行流程发布 0.3.9 并核验固定摘要，根任务负责 home 重装、原生归属释放与部署验收。

## 验证目标

覆盖摘要不符、无归属、仍选择字段漂移、未恢复基线漂移、正常 apply 拒绝漂移；成功释放后保留原 OpenAI 配置和其他字段，归属可重复检查。覆盖旧 feature 的受管迁移和非受管保留。运行完整源码测试与真实发行载荷测试；不发送模型请求。

## 实际交付

- 发行源码提交 `7fefb84f8bbf5e161a717c76290fe50cc73d8d53`，组件 main 与 v0.3.9 均已推送。完整源码测试 254 项通过，2 项按环境跳过；另行构建真实载荷并运行隔离安装/失败恢复/重装测试，1 项通过。原生 Paseo 测试未选择，与本次修复无关。
- [发行 CI 43596444918](https://github.com/PennixRv/pennix-lifecycle/actions/runs/43596444918) 成功，源码、实际载荷和发布步骤均通过。[v0.3.9](https://github.com/PennixRv/pennix-lifecycle/releases/tag/v0.3.9) 正式包 SHA-256 为 `9fcd2ed4354e7c0b6955285922db92b890d8ff466d8cc186911e5e17f5beade4`，与正式校验单一致。本地 gzip 包摘要为 `88417f95682ea430ca21c79d275d8e663188fd4b41b946a57e898612ed6cacf2`；压缩器输出不同，解压 tar 逐字节相同，SHA-256 均为 `55d65875e20644a8a7f6493aace3e10fbb2ec818c99afa03ccd6284a3053be47`，52 个成员内容、模式与归档元数据一致。部署使用正式资产。
- 首次本地载荷测试误用了不含 pip 的已安装运行环境，在下载测试轮子前失败；改用具备 pip 的系统 Python 执行同一隔离测试后通过。没有向 Lifecycle 运行环境安装测试工具或修改其依赖。
- home 通过已核验正式源码的原生 tool install 重装为 0.3.9，旧 0.3.8 目录由 owner 回收。workflow.config 选择 PRESERVE，按完整配置摘要执行原生 repair，旧 cchp provider 归属释放，旧 feature 移除。整个 TOML 语义比较只少了旧 feature，用户 OpenAI/CCHP/认证/500K/V2 均保留；再执行 repair 为 no-op，inspect=current、plan=keep、recover=idle。
- 未放宽保留名限制，未手写归属文件，未改会话历史，未重启当前 daemon，未升级其他组件。全局 plan 中原有 aoe/codex-cli 阻断独立保留，不将本次 codex-config 通过宣称为全工作流 check 通过。
