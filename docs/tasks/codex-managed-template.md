# Codex 受管模板与双机统一

根任务为 codex-workflow-optimization 的 `11-18-codex-compaction-cache-latency`，用户已确认完整合同并批准实施、发布和 Pennix／PennixLaptop 部署。源码沿 main，不新建分支或工作树。

补齐既有 Codex owner 的声明式管理：增加显式选择的受管模板模式，对具体原生默认保证键缺席；保留旧 NATIVE／PRESERVE 语义，普通 apply 继续拒绝漂移。共同模板包括 OpenAI/CCHP、Sol、500K／450K／total、high 等已定配置，中央秘密不进入源码。

修改范围为 Kconfig／defconfig、Codex owner、配置解析与 renderer、原生配置接管必要适配、既有测试与版本。旧压缩符号仅兼容读取，输出退役；移除说明性注释，保留 Kconfig 机械表达和 TOML 语义。不修改代理服务，不迁移会话、凭据或数据库，不增加搜索／模型／压缩验证。

验证复用 tests/test_config.py、认证／归属／事务测试及正式发行的完整隔离测试与实际载荷测试。初轮配置专项 56 项通过；补充“值已匹配仍须证明完整归属”的回归后，最终隔离环境完整套件运行 339 项，无失败、跳过 4 项。其中独立 Copilot／Paseo／CodeGraph 原生探针未选择，实际发行包测试待构建归档后单独执行。正式发行后两机通过原生工具落地，核对值、缺席、owner current、幂等计划及 recover。双机落地证据由根任务 execution.md 记录，当前尚未发布部署。
