# Pennix Lifecycle

`pennix` 管理 Pennix 工作流的用户级配置和已登记组件。安装器本身位于
`~/.local/share/pennix-lifecycle/releases/<version>`，入口位于 `~/.local/bin/pennix`。

bootstrap 是独立 Bash 入口：它准备 Arch 基础依赖，从固定 GitHub Release 下载运行包并校验 SHA-256；无需 clone 仓库、Codex 或已安装的 Skill。普通用户执行：

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://github.com/PennixRv/pennix-lifecycle/releases/latest/download/bootstrap.sh | bash -s -- install
pennix config init
pennix config edit
pennix plan
pennix apply
```

可用 `--release vMAJOR.MINOR.PATCH` 固定版本；root 调用必须显式传入 `--user USER`，工具自身卸载使用 `bootstrap.sh uninstall`。bootstrap 只准备工具运行依赖，不安装 AUR helper 或工作流组件。

支持范围为 Arch Linux / WSL2 Arch、x86_64、zsh 自动 PATH 管理。bootstrap 会先执行完整 `pacman -Syu`，安装系统 Python 及 Kconfiglib、TOML、YAML、base-devel 等运行依赖；不以局部依赖升级替代完整系统准备。普通用户的 HOME 必须与目标账户一致；root→用户调用清空宿主环境。自定义 XDG 目录可在普通用户入口保留。

同设备同用户仅维护一个目标，允许非默认 CODEX_HOME。共享锁和目标登记不随 XDG_STATE_HOME 改变；切换状态目录或目标会阻断，不能绕过已有事务。跨设备只传递 `workflow.config` 的配置意图；目标机重新解析包身份并建立本机锁、receipt、秘密和 journal，不复制这些运行记录。

新机可直接使用仓库内的公开 WSL 默认配置生成主机配置；主机专属 URL、ID 和路径
在生成后编辑，秘密仍单独填写：

```sh
pennix config init --defconfig /path/to/pennix-lifecycle/config/defconfig-wsl
pennix secrets edit
```

已有可信 SSH alias 时，也可以按服务选择性导入固定 owner 凭据文件。该快捷方式只
读取登记的凭据字段，不复制认证文件、URL、ID 或路径：

```sh
pennix secrets import-ssh --alias office-wsl-via-nas --service codex
```

`workflow.config` 是带格式主版本的 Kconfiglib 配置文件。`config edit` 和无 TTY
的配置文件路径使用同一个模型；`plan` 只读，`apply` 才改变组件。一次性
`uninstall <component> --confirm <component>` 不修改配置；要永久取消组件，先在
`config edit` 中取消选择，再执行 `apply`。

文档阅读可选择 `PENNIX_DOCUMENT_READING` 分组：MarkItDown 的四项 extras 和固定附加 Python 库共用一个 uv tool 环境，配合 Pandoc、LibreOffice、Poppler、中英文 Tesseract 数据及 Noto CJK 字体。各依赖仍可独立选择；默认配置不自动开启此组。现有外部系统包只验证原生包名、来源及可执行文件归属，保持 external，不写接管 receipt，也不因取消选择而卸载。首次缺包遵守完整系统升级门禁。

`package.extras` 是 uv extra 名称数组，`package.with` 只接受 `name==version` 固定需求，`package.imports` 映射所需模块和发行包名。安装、升级与恢复都从同一 catalog 生成参数；状态检查核对 uv receipt、入口归属、实际导入及固定版本。`package.identity_only=true` 仅用于无可执行入口的官方系统包；`external_satisfies=true` 仅允许已核验的外部官方依赖保持原归属。`resolve_dependencies=true` 允许 Pandoc、LibreOffice 和字体在已确认完整系统升级的边界内，使用 pacman 对固定签名本地包作原生依赖预览和安装；共享间接依赖由 pacman 管理，不伪造单独 Lifecycle 归属，也不在回退时递归删除。普通 npm/uv 包和有原生配置的组件不能使用这些系统包策略。新增组件还须登记 APPLY_ORDER、候选解析器和 Kconfig/defconfig，现有闭合集合测试负责发现遗漏。

用户 Skill 集合保留 `pennix-skills` 和 `pennix-skills-extra` 层级，统一安装到
`$HOME/.agents/skills`，同一用户的宿主配置共用这份内容；CODEX_HOME 继续管理
配置与认证，不选择集合版本。旧 CODEX_HOME/skills 集合由 `pennix repair <collection>`
在原锁定提交迁移，`pennix update <collection>` 单独升级来源。plan/status 显示
`migration-required` 时先 repair；未知归属、内容漂移和新旧目标冲突拒绝覆盖。
迁移中断保留私有恢复副本及 journal，由 `recover --resolve completed` 核验并续接。
取消一个配置中的集合选择保留共享资源；显式卸载集合会影响此用户的全部宿主配置。
Context Mode 的三个命令 Skill 由其原生计划渲染，Lifecycle 按新旧回执核验和迁移，
卸载单个宿主接入时保留可能仍由其他宿主使用的共享 Skill。

与 `workflow.config` 同目录的 `components.lock.json` 记录每个动态组件已核验的
来源、版本、提交或发布引用和摘要。它由 Lifecycle 原子写入，不应手工编辑。首次
安装缺少锁时，`apply` 只解析一次当前候选，完成安装和后置核验后写入锁；已有可信
receipt 会先回填锁。已有安装缺少可信 receipt 时，`apply` 会阻断并要求先完成归属
迁移。普通 `apply` 按锁执行，不自动升级；明确升级使用
`pennix update <component>`，该命令先展示精确候选和影响，再在同一次交互确认后
执行单组件替换。

Arch/AUR 在外部升级后，使用 `pennix reconcile <component>` 显式接受当前精确身份。
它核对既有 receipt、旧锁、包名和可执行文件 owner，只同步记录，不安装、升级或降级。
Codex 插件亦可沿该入口保留当前已启用版本，以原生 Marketplace API 对齐相同源的精确
提交；脏树、未知归属、其他插件受影响或确认后候选变化均阻断。receipt 与锁部分写入
失败时保留 journal；相同入口只在实际身份仍吻合时恢复，不盲目重放未知原生副作用。
`outdated` 保留兼容字段，表示实际身份与锁不相等，不代表实际版本更旧；输出另列实际
身份、锁定身份及既有受管记录。

npm 组件可用 `pennix update trellis-cli --version 0.7.1-beta.0` 选择精确版本，仍由
登记的 registry 校验版本、包名和完整性，并沿同一次确认、事务、锁及 receipt 完成
替换；不修改组件的默认通道。此参数不接受 tag 或版本范围，也不适用于非 npm 组件。
恢复未完成的 update 时，指定版本必须与事务已确认目标一致；省略参数仍恢复原目标。

启用 `PENNIX_SYSTEM_UPGRADE` 时，`apply` 会在完整计划中显示并确认
`sudo pacman -Syu --noconfirm`；缺少或更新 Arch/AUR 组件时，必须沿这条同次确认的
路径执行。Lifecycle 变更会在私有状态目录写入 journal，步骤只有完成后置核验才标记
为 verified；副作用状态未知时保留 journal 并阻断自动重试。`update` 只能在当前
owner 的完整恢复事实成立时恢复原先确认的目标；版本相同不足以释放 unknown。
repair 和 uninstall 同样写持久事务。系统升级后只刷新原生 official 包记录，重新计划剩余步骤；不为外部改变的 AUR 包补造受管记录。

`pennix recover --json` 只读展示事务。带 `--transaction <id> --step <id> --resolve <not-started|completed|restored>` 时，在同一目标锁内重新核验：未执行须由 pending 状态或原生 none 回执证明；restored 须有同一步骤的恢复事实；completed 须证明原生后置条件并补齐必要锁记录。处置归档保留结果；取消或恢复前值的步骤不代表目标已应用。没有 force 清除未知副作用的入口；证据不足保留现场。
若旧 `apply` 仅剩 Paseo 服务步骤为 `started` 或 `unknown`，且配置、锁未改变，
配置步骤已核验，当前 unit、daemon、配置及 Skills 同步均达到期望，重新执行 `apply`
只核验并结束该旧事务，不重启服务，也不执行新计划；其他未知步骤仍保持阻断。

秘密位于 `secrets.toml`，由 `pennix secrets edit` 打开并以明文编辑，文件权限为
0600；其中只保存凭据字段，普通 URL、ID 和路径属于 `workflow.config`。JSON 输出
不会包含秘密值。

Windsurf 可显式开启 `PENNIX_WINDSURF_OWNER_CREDENTIAL`，不再要求中央 `windsurf.api_key`。
缺专属文件时由 Windsurf owner 原生 Devin resolver 导入；秘密不经过 Lifecycle 进程。
既有中央模式保持兼容。外部文件、漂移、环境覆盖或不安全权限会阻断，不自动接管。

连接 owner 通过原生 validate 复用自己的输入与安全校验，声明协议能力，执行回执绑定事务步骤身份。validate 不创建目录、不写 receipt、不请求网络；超时、旧协议、错误身份或响应不完整均保留 unknown。孤立 receipt 只有在目标缺失和归属可证时才受控撤回，不把外部文件当成自有文件。取消 Windsurf 连接只移除自有凭据副本和 receipt，保留中央输入与 Devin 登录。

Paseo 自启 unit 只在用户 manager 启动时执行一次原生 `daemon start`，不持续管理 daemon。
登录自启无需开启 `PENNIX_PASEO_RUN`；该键只表示本次 apply 的明确启动请求。
日常启停使用原生 `paseo daemon start/stop/restart`；关闭入口不停止 daemon，普通
apply 不复活已停止实例。自启配置、daemon 健康和版本分别核验；停止时运行版本为
`not_run`，依赖实际运行的操作仍需健康条件。旧前台 unit 仅凭精确 receipt 识别迁移，
实际承载 daemon 时须活动保护；当前独立 daemon 不为匹配 unit 而重启。

Paseo 官方 Skills 使用已验证的 CLI/client/server 0.15.3 公开 SDK。首装自动调用原生安装接口，覆盖 `.agents/skills`、`.claude/skills` 及原生自有的旧 `.codex/skills`；原本停止的 daemon 仅为该动作临时启动，并按同一实例停止。新 CLI 候选先在隔离前缀暂存、检查公开接口，再改变用户安装。安装或启停结果未知时保留已提交字段与事务，不恢复旧前台启动造成潜在双实例。远端 relay 登记和本地 daemon relay 配置分别记归属；register=n 仅表示不访问远端，不表示本地配置已撤回。

Context Mode 通过 `@pennixrv/context-mode` 包、Codex 原生 MCP 和六个 Hook 接入；不叠加 plugin，也不改写用户 AGENTS。旧 FastCtx 的撤回由旧安装的原生入口完成，新配置格式 5 不接受旧组件键或旧格式。

本地 Hikari wrapper、官方 `tvly`、OpenPeon 运行文件和 AoE/OpenPeon 的精确 Codex Hook 由各自登记合同管理。所有 NAS/其他主机的服务端均为外部依赖，不安装或升级。Hikari 只取得固定上游客户端，并由 Lifecycle 生成本地 wrapper，不执行上游 installer 或 fork 源码；OpenPeon 只更新有限运行文件，保留音效包、偏好和运行数据。CCH 使用经过摘要校验的私有 GitHub Release npm 包，需要现有 `gh` 认证。

正确的既有包可通过 `PENNIX_ADOPT_EXISTING_PACKAGES` 显式纳管，接管前核对原生包名、版本和命令归属；更新须使用 `pennix update <component>`。对无 receipt 的客户端既有文件，先审阅 `plan` 的摘要，再用 `repair <component> --expected-sha256 <摘要>` 精确迁移，未知旁侧文件保留。

`check` 将本地安装/配置结果与功能请求分开输出。`PENNIX_FUNCTIONAL_CHECKS` 默认关闭；明确开启后才运行已选连接的原生功能入口，Windsurf 仅使用临时合成项目。失败、超时或没有入口均不能通过，不回显响应正文。SiYuan 的实际 MCP 会话连接仍由宿主原生工具核验，不能用本地 receipt 代替。

工具升级成功后只保留当前发行。完整、归属和权限安全的旧安装目录可清除；未知文件、符号链接和漂移会阻断。AGENTS 的正常升级要求归属凭据中的摘要仍匹配；人工审阅的迁移可用 `pennix repair codex-agents --expected-sha256 <完整文件摘要>` 精确确认当前字节，摘要不符不会写入。

发行 tag 和资产不可覆盖。同版本失败重试只复用摘要完全匹配的已暂存发行；工具卸载保留其他 CLI 仍需的 PATH、共享 linger、用户数据、组件归属及未结事务元数据。`tests/test_release_payload.py` 以实际构建载荷验证隔离 bootstrap、入口恢复和重装；Arch 包、下载使用明确替身，不代表真实新机、跨登录、PAM 或所有外部服务已经实测。
