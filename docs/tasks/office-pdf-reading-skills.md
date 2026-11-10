# Office 与 PDF 阅读运行时

执行根任务 office-pdf-reading-skills 已批准的 WP2/WP3，分支 main。用户对包含实施、发布、安装和 Git 收尾的完整方案答复“认可”；本记录承接真实授权，不扩展范围。

在现有 package owner 中支持 uv extras 和固定附加包，沿相同参数安装、升级和恢复；校验 uv receipt、命令归属与必要导入。为无可执行入口的 OCR 语言包和字体支持原生 pacman 包身份。登记文档阅读配置分组，保留既有外部包归属，使用原生计划和状态，不自动接管。

先完成适配与有意义的回归，再固定已推送的主集合提交、正式发行并核验载荷，通过原生 update/config/plan/apply 安装。系统升级、sudo 或归属拒绝按原生流程停止；不临时 pip 安装或手改锁/receipt。恢复沿同一 journal/recover，保留已有外部包。

## 源码验证与发行候选

固定主集合 1fab5b4ebaf20ec0af8627ad7289631a7f469acd。原有组件规范无新增 Trellis 资产；README 已记录 extras/with/imports、identity_only/external_satisfies 的字段及错误边界。受影响入口为 host 安装参数、engine 身份/probe/receipt、plan 外部依赖策略、候选解析器、动作顺序及配置；没有新增安装器或第二状态系统。

全量 unittest 205 项通过，2 项按原条件 skipped；其中新增 6 项覆盖固定参数、同参数重试、安装失败不写 receipt、extras/版本/导入拒绝、无命令语言包及外部包保留/卸载拒绝。隔离真实 uv 安装和身份核验通过，MarkItDown 0.1.9 wheel SHA-256 为 52f59c48bea9cf887224c56247f648d68beb7aa7af5a12d414748210c83c20ea。正式发行候选 0.2.48，尚待正式 CI、载荷与用户环境部署验证，不把源码通过记为正式生效。

0.2.48 正式发行与载荷测试通过，已安装工具、集合和 MarkItDown；原生 apply 在 Pandoc 候选阶段停止，未开始该包变更。实际 pacman -T 的缺依赖状态为 127，旧适配器只认 1；原生 pacman -U --print 可解析固定本地包及同步仓库依赖。修正该判断，仅为三项新文档系统依赖启用受完整系统升级门禁保护的原生依赖解析，保留签名、主包摘要和身份核验，不引入第二依赖安装器。Pandoc 当前闭包 239 项，三项合计原生预览 292 项，主要为 Arch pandoc-cli 的 Haskell 依赖；这是已批准完整本地方案的实际依赖影响，不改为其他来源。
