# Office 与 PDF 阅读运行时

执行根任务 office-pdf-reading-skills 已批准的 WP2/WP3，分支 main。用户对包含实施、发布、安装和 Git 收尾的完整方案答复“认可”；本记录承接真实授权，不扩展范围。

在现有 package owner 中支持 uv extras 和固定附加包，沿相同参数安装、升级和恢复；校验 uv receipt、命令归属与必要导入。为无可执行入口的 OCR 语言包和字体支持原生 pacman 包身份。登记文档阅读配置分组，保留既有外部包归属，使用原生计划和状态，不自动接管。

先完成适配与有意义的回归，再固定已推送的主集合提交、正式发行并核验载荷，通过原生 update/config/plan/apply 安装。系统升级、sudo 或归属拒绝按原生流程停止；不临时 pip 安装或手改锁/receipt。恢复沿同一 journal/recover，保留已有外部包。

## 源码验证与发行候选

固定主集合 1fab5b4ebaf20ec0af8627ad7289631a7f469acd。原有组件规范无新增 Trellis 资产；README 已记录 extras/with/imports、identity_only/external_satisfies 的字段及错误边界。受影响入口为 host 安装参数、engine 身份/probe/receipt、plan 外部依赖策略、候选解析器、动作顺序及配置；没有新增安装器或第二状态系统。

全量 unittest 205 项通过，2 项按原条件 skipped；其中新增 6 项覆盖固定参数、同参数重试、安装失败不写 receipt、extras/版本/导入拒绝、无命令语言包及外部包保留/卸载拒绝。隔离真实 uv 安装和身份核验通过，MarkItDown 0.1.9 wheel SHA-256 为 52f59c48bea9cf887224c56247f648d68beb7aa7af5a12d414748210c83c20ea。正式发行候选 0.2.48，尚待正式 CI、载荷与用户环境部署验证，不把源码通过记为正式生效。

0.2.48 正式发行与载荷测试通过，已安装工具、集合和 MarkItDown；原生 apply 在 Pandoc 候选阶段停止，未开始该包变更。实际 pacman -T 的缺依赖状态为 127，旧适配器只认 1；原生 pacman -U --print 可解析固定本地包及同步仓库依赖。修正该判断，仅为三项新文档系统依赖启用受完整系统升级门禁保护的原生依赖解析，保留签名、主包摘要和身份核验，不引入第二依赖安装器。Pandoc 当前闭包 239 项，三项合计原生预览 292 项，主要为 Arch pandoc-cli 的 Haskell 依赖；这是已批准完整本地方案的实际依赖影响，不改为其他来源。

0.2.49 官方 CI 42786846260 成功，官方资产 SHA-256 为 057a37ddf0840de8c6e4a767f16f18dccd11663cba90f1a371c43187221c92a5。工具升级被 pending journal 正确阻断，改用已核验正式发行的原生 apply 续同一事务，Pandoc、LibreOffice 安装并验证。字体候选遇 404，定位为 any 架构包被固定查询 x86_64；为字体和语言数据登记 architecture=any，候选及固定锁恢复均使用真实架构，仍只支持 x86_64 宿主。回归覆盖 any 元数据和带 epoch 的恢复文件名。修复按原生正式发行 0.2.50 交付，不改写已发布 tag 或资产。

官方语言包元数据另确认 filename 含 epoch（如 tesseract-data-eng-2:4.1.0-5-any.pkg.tar.zst）；旧通用文件名校验拒绝冒号且锁恢复剥除 epoch。仅 Arch 包名校验允许冒号，仍拒绝路径分隔和目录穿越，其他候选维持原校验；恢复保持真实 epoch，补入原架构回归。最终发行收敛至 0.2.51；字体未开始安装，保留同一 journal。

## 正式部署验收

0.2.51 源码 b138ea6e57465dd89d0631297ef4ab571a5a0f1e，官方 CI 42788130914 成功，包含隔离全量 208 项（2 skipped）及实际发行载荷恢复和重装测试。正式资产 SHA-256 为 375b5a3171c77170ce1f90074e57d3ca0470c95a0926913dc331d40a542698d1，核验后用该正式入口继续原事务。Noto CJK 安装完成，recover 返回 idle 后原生 tool install 成功切换 0.2.51；没有手改运行状态。本次完整系统升级后恢复原有默认不升级配置。

正式 status/check 均成功，文档八项及主集合为 match。MarkItDown 环境导入和固定版本核验通过，Pandoc 3.11-58、LibreOffice 26.8.2-1、Noto CJK 20240730-1 已安装；已有 Poppler、Tesseract 和语言包仍保留外部归属。原生功能检查未请求，由主集合实际 smoke 验证完整本地能力：四格式读取、公式/缓存区分、中文 OCR、页面渲染、旧格式转换、无效 PDF 拒绝及原件哈希不变全部通过。OCR 输出存在误字，不承诺逐字准确。根组合门禁及真实模型发现由根任务保存最终证据。
