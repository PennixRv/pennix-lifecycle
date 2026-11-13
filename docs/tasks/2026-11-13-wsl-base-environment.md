# WSL开发基础环境与配置交付

## 授权与基线

根任务pennix-wsl-base-environment已接受实施合同并取得用户“批准完整推进”的授权。组件基线0c1b4da（0.2.58），原现场位于fix/existing-project-spec-convergence。用户指出须遵守既有分支与发布规则后，已核验历史明确约定及.gitmodules：组件在既有main工作树维护，不新建分支或工作树。main已无损快进到相同基线，所有未提交改动逐文件核验字节与权限不变；本轮误建的feat/wsl-base-environment已删除，未推送。根任务保有整体决策和验收，本文件记录组件实现和验证，不建立第二套任务状态机。

发布沿用既有正式 tag CI，验证固定源码、公开载荷及实际安装身份；不覆盖或移动已发布 tag。原方案中“建立功能分支”的安排被用户本次明确纠正覆盖，不作为执行依据。随后已完成全仓与子模块同步审计；用户“批准完整推进到所有工作完成和git收敛”，恢复当前实施合同，不新建分支或工作树。

## 变更边界

当前缺口是工具引导依赖无人维护AUR、没有正式基础环境适配器及配置投递合同、PATH只覆盖.zshrc，以及pnpm来源与目标不符。行为归属在bootstrap/release owner、配置模型/CLI、基础适配器、path_env、共享catalog/tmux owner及正式发布链。

修改范围：bootstrap.sh及纯标准库安装入口/发行运行环境；config/Kconfig与profile兼容；scripts/pennix.py、_engine.py和现有事务分派；专用base_environment及references清单；templates内审查后的P10k/Shell/编辑器/Git公共配置；path_env和pnpm/文档Python现有owner；对应tests、README和发布文件。复用owned_files、锁、lock/receipt/journal，不新增通用模板框架、配置同步器或事务系统。不自动部署全部工作流组件，不修改SSH/NAS/Windows拓扑，不复制用户秘密和运行数据。

W1 隔离实现及回归，W2 正式发布与公开载荷身份，W3 新机 pennix，W4 存量 home，W5 根集成、清理及思源文档。依次执行；以下验证记录区分源码、发行载荷与真实设备。

## 验证记录

- 修改前完整隔离unittest：228项，19.253秒，OK，2项原有skip。
- 后续定向/完整回归、公开载荷及设备验收结果随实施追加。

## W1 实现

新增显式 development profile 和 base scope；复用现有组件 owner、锁、receipt 与 journal，交付稳定基础配置和私有哈希锁定工具环境。基础包使用官方仓库及经过维护状态检查的 AUR 包；Node.js 24 LTS、官方 pnpm、系统 Rust/Cargo 和 Go 使用统一 PATH。旧 npm pnpm 迁移保留精确旧归档，支持同事务续接或恢复。配置接管绑定审阅过的摘要，保护用户修改和外部内容。

P10k 从已审阅的 home 配置提取全部 439 行有效代码及必要作用域、函数和自定位逻辑，剔除原注释体和缓存，不复制认证或机器身份。原文件 SHA-256 为 `65e00e42e4e9e95e6ff80b16248b7e8be1af582f36aae37fa47b79c0aadb69b1`；保留上游 MIT 许可。

Node.js 24.22.3 的官方 Arch 包已通过签名核验，并在临时目录运行现役主要 CLI；Paseo 与委派适配器的六个 Linux 原生模块均可加载。官方 pnpm 12.19.0 在隔离项目中识别并使用 packageManager 指定的 pnpm 10.32.1。两项预检均未修改真实系统包和项目锁。

完整隔离回归最终为 245 项，2 项按既有原生 Paseo/显式发行载荷条件跳过；bootstrap 参数、Bash/zsh 语法及差异检查通过。显式构建的实际 tar 另行通过无宿主 HOME、无网络命名空间测试，覆盖首次引导、私有依赖安装、切换后回执失败、原入口保留、同版本复用、卸载和重装。实际载荷发现并修复了 venv 内部链接权限误判，以及 uv 固定生成 0666 环境锁的问题；只收紧新建工具环境的锁权限，仍拒绝外部链接和未知可写文件。此记录不代表设备部署已完成。

## W2 发行记录

`v0.3.0` 的正式 CI（43395185775）在源码检查阶段失败，没有生成公开 Release 或部署设备。Ubuntu runner 缺少新增测试要求的 zsh，且源码入口固定使用系统 Python，绕过了 CI 的开发虚拟环境。已补齐 runner 的显式依赖，并恢复源码入口使用 PATH 中的 Python；安装后的正式工具仍优先使用私有解释器，bootstrap 仍由固定系统解释器建立它。保留原标签和失败证据，使用新版本 `v0.3.1`，不移动标签。

`v0.3.1` 的 CI（43395508061）已完成源码、真实载荷与正式发布。部署前逐项复核补齐了 Neovim 默认编辑器的 `EDITOR`/`VISUAL` 环境配置；此前只有 Git editor 已设置。此补齐使用 `v0.3.2`，两台设备使用同一个最终公开版本，不现场手补模板。

`v0.3.2` 的 CI（43395870340）与公开资产独立载荷测试通过；公开 tar 的 47 个源码文件内容和可执行位与 tag 一致。pennix 首次真实引导已安装该工具，但基础计划暴露 bootstrap 自身安装的 uv 缺少 receipt、被旧锁迁移门禁阻断，以及 base scope 文本计划缺少 PATH/系统升级显示字段的问题；没有执行基础 apply。修复版本为 `v0.3.3`：复用原有 external_satisfies 策略验证官方 uv 并保留系统归属，base scope 显示完整计划且不另写旧 PATH 段；完整范围在选择基础能力时同样不生成旧 PATH。新增真实文本计划渲染及无 receipt 官方 uv 的回归，不制造归属。

`v0.3.3` 的 CI（43397686638）通过 247 项源码测试（2 项条件跳过）及实际载荷门禁；公开载荷独立测试通过。目标完整系统准备和官方 pnpm 安装后，tmux 候选预检发现缺少 libutempter；失败发生在 tmux 写入前。已逐项核验其余步骤均 pending 且没有 observed，由原生 recover 将这些未启动步骤按 not-started 收束，保留系统升级和 pnpm 的已验证结果，没有手改 journal。

`v0.3.4` 修复干净环境的 tmux、Poppler、Tesseract 及语言包原生依赖解析，继续要求同次完整系统升级。已选英文/中文语言包先交给 pacman 解析相互依赖并提供 tessdata，避免自动选入无关语言。基础能力已选择时不再登记多余旧 PATH 步骤。延续既有 owner、签名验证、原生依赖预览和 journal。

目标使用 0.3.4 完成 tmux、受管 tmux 配置及 MarkItDown。Pandoc 依赖下载出现镜像 TLS/超时，pacman 明确未提交安装，原事务将 Pandoc 保留为 unknown。0.3.5 补齐原生 completed 恢复：仅对仍然缺失的官方包安装步骤，在同一事务完整系统升级已验证时，重新获取冻结版本及摘要的签名候选，经原 owner 安装并核验归属后写锁。已存在但无归属的包不借此自动接管；失败保留 unknown。增加缺失包成功、缺少升级证据和未知外部包三种回归。基础计划同时显示模板来源及版本。

0.3.5 的 CI 43403986712、249 项源码回归及独立公开载荷测试通过。目标经原生恢复完成 Pandoc，再安装 0.3.5 继续 apply。基础官方包、yay 和文档组安装完成后，AUR 两个 GitHub 源出现目标侧 TLS EOF；保留基础 receipt 和同一 journal，经临时 SSH 回环 SOCKS 下载通道重试原始 HTTPS 源及 PKGBUILD 校验，恢复成功后立即关闭通道，无持久代理配置。

真实验收暴露 P10k 模板的参数清理会移除之前设置的禁用向导标志，0.3.6 在加载后恢复明确策略；同时将基础 owner 错误交给既有 CLI 错误处理，保留事务摘要，避免裸 traceback。临时验收脚本另修正三个测试前提：独立 XDG 目录须具备 env.sh、Git 显式 --global 读取 include 须加 --includes、OCR 字间空白不视为识别失败；这些修正没有改动部署配置。

容器构建验收同时收紧为默认的 BuildKit 路径：基础清单补齐同属 Arch 官方 Docker 工具链的 docker-buildx 0.38.2（仅依赖既有 glibc），不以 DOCKER_BUILDKIT=0 强制旧构建器作为最终证据。不增加第三方仓库、服务或外部镜像，仍使用本地 scratch 示例及既有 daemon。
