# Codex 会话身份兼容与旧 feature 归属收敛

承接根任务 `11-14-codex-proxy-cache-compaction-investigation` 的已批准实施修复，沿用组件 main，不创建分支，不回退根任务阶段。

## 范围与实施合同

历史会话绑定 `model_provider` ID。home 已由用户恢复为 `OpenAI`，CCHP URL、私有认证、500K 和 provider V2 保持。Lifecycle 对此选择 PRESERVE；不放宽内置保留名校验，不修改会话元数据。

复用 `repair codex-config --expected-sha256`：摘要必须匹配完整当前文件，已有可信归属必须存在。只允许释放不再选择、且人工已恢复首次基线的漂移字段；仍被选择的字段或未知漂移继续拒绝。正常 apply/inspect/uninstall 的漂移保护不变，不提供任意接管或 force 入口。

停止生成已移除的 `features.remote_compaction_v2`；旧 Kconfig bool 暂保留为兼容输入，避免旧配置成为未知选项。仅在可信旧归属存在且字段通过漂移检查时移除旧 feature，不删除用户外部维护的同名字段，不改 provider 的能力声明。

修改 codex_config owner、repair 参数归属、Kconfig 提示、回归测试和发行版本。使用现有 README 的发行流程发布 0.3.9 并核验固定摘要，根任务负责 home 重装、原生归属释放与部署验收。

## 验证目标

覆盖摘要不符、无归属、仍选择字段漂移、未恢复基线漂移、正常 apply 拒绝漂移；成功释放后保留原 OpenAI 配置和其他字段，归属可重复检查。覆盖旧 feature 的受管迁移和非受管保留。运行完整源码测试与真实发行载荷测试；不发送模型请求。
