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

支持范围为 Arch Linux / WSL2 Arch、x86_64。bootstrap 会先执行完整 `pacman -Syu`，准备 curl、CA/keyring、系统 Python 和 uv；Kconfiglib、TOML、YAML 按发行内的完整哈希锁安装到工具私有 `.runtime`，不再要求系统 Kconfiglib/AUR 包或全局 pip。不以局部依赖升级替代完整系统准备。普通用户的 HOME 必须与目标账户一致；root→用户调用清空宿主环境。自定义 XDG 目录可在普通用户入口保留。发现未结事务时 bootstrap 先停止，不能用重装清除 unknown。

## 主动项目初始化策略

`PENNIX_PROJECT_INIT_CODEX_TRUST` 和 `PENNIX_PROJECT_INIT_CODEGRAPH` 默认关闭，可在现有声明中分别开启。具备对应能力的 `trellis init --codex` 读取该策略，自动接入明确项目的 Codex 信任、已核验 Trellis Hooks 和 CodeGraph local／索引；显式关闭、未知配置及修改过的受管脚本仍保留。系统安装、普通会话和目录创建不触发项目初始化，也不安装 Git 同步 Hook。

`pennix config policy --json` 只读返回版本化的两项布尔策略，不输出完整配置或秘密，不查询包锁和组件运行态。缺少项目能力由 Trellis 明确报告，系统部署和项目操作仍分属原生 owner。

## 网页搜索退役迁移

新配置默认明确选择 Codex `web_search="live"`；“保留现有配置（不接管）”不会清除旧的 `disabled`。Copilot 仅使用其原生 `web_fetch`，不承诺独立搜索。Windsurf 代码定位保留。

显式配置自定义 Responses provider 且选择 Live／Cached 搜索时，同时写入原生 `supports_standalone_web_search=true`。Codex 0.169 的搜索扩展要求这一能力声明；仅写入 `web_search` 不能证明工具已暴露或后端可用。后端仍须支持该原生接口，按新CLI／Paseo实际搜索验证；保留已有provider模式不改写其元数据，关闭或不接管搜索时恢复该字段的原始基线。

已有部署须先用旧正式 Lifecycle 取消三条搜索连接及两个客户端，确认原生撤回完成，再从公开配置中移除退役键、安装新工具并更新集合。新版本不再安装网页搜索客户端；中央旧秘密保持只读兼容，不再导入或列为必填。

旧 Grok owner 会留下普通偏好，旧客户端可能留下空目录或早期隐藏载荷。新发行提供一次性原生清理脚本 `scripts/retire_web_search.py`：先预览，再以 `--yes --config-sha256 <预览摘要>` 执行。仅处理固定旧目录中无凭据的已知偏好、空目录，以及固定来源摘要一致的 `~/.local/bin/.tvly-hikari-real`；receipt、未知文件、符号链接、待恢复事务及摘要不符均阻断。它不清理中央秘密、NAS 服务或历史，也不提供搜索安装入口。

## 开发基础环境

普通工具安装保持最小默认。显式选择开发基础环境时，使用同一固定发行的公开 bootstrap：

```sh
bash /path/to/bootstrap.sh install --release v0.3.8 --profile development --git-name 'Your Name' --git-email 'you@example.org'
```

这会生成或合并 `development` profile，展示基础范围计划，再要求确认完整系统升级与应用。Git 白名单身份只绑定在目标机，不带入认证。已有工具可使用：

```sh
pennix config init --profile development --git-name 'Your Name' --git-email 'you@example.org'
pennix plan --scope base --json
pennix apply --scope base
pennix check --scope base --json
```

新配置只选择基础能力、共享 pnpm/uv/tmux/tmux-config 和文档组；已有配置仅合并这些选择，保留其他组件和设备专属值。`--scope base` 不配置、卸载或启停范围外的工作流集成和 Paseo。基础包清单由 `references/base-packages.json` 唯一维护；软件继续归 pacman/AUR 数据库，不建立机器级全依赖锁。系统使用 Node.js 24 LTS、官方 pnpm、系统 Rust/Cargo 和 Go，不增加语言版本管理器。AUR 候选必须有维护者且未被标记过期；Oh My Zsh 的 VCS 版本由原生 `pkgver` 生成。

稳定公共配置位于 `$XDG_CONFIG_HOME/pennix/base`。Bash 登录文件和 `.bashrc` 共用 `env.sh`，systemd 用户服务使用 `environment.d`。PATH 保留项目 venv 优先级，去重并移除旧 `.npm-global`、无来源 `~/bin`、Windows PATH 及 Codex 临时注入路径。直接执行的非登录 Bash 使用其启动方提供的环境。只有交互且有 TTY 时加载 Starship、ble.sh、bash-completion、fzf 和 zoxide。个性化配置可使用 `bashrc.local.bash`、`STARSHIP_CONFIG` 和 `nvim.local.lua`。Yazi 通过 `PENNIX_COMPONENT_YAZI` 显式选择，默认关闭。

ble.sh 在输入停顿 150 毫秒后以暗灰字显示历史联想，沿用终端背景。按 Tab 打开补全菜单，每页最多 6 行。Bash 窗口通过 ble.sh 原生方式共享历史，未加载 ble.sh 时使用 Bash 内建的历史同步。

plan 展示目标、前后摘要、接管对象和原生设置。迁移已有 Zsh 基础环境时，Lifecycle 更新其受管启动段并保留用户文件。对经审阅的旧受管 Shell 启动文件漂移，将目标绝对路径与当前 SHA-256 写入 `PENNIX_BASE_ADOPT_FILES` 的 JSON 映射。无法匹配已审阅值的差异、符号链接和错误标记会阻止应用。Git 通过原生 include 加载公共偏好与私有身份，保留项目覆盖和认证。tmux 仅升级可证明的上一版受管段，保留 CCH 与外部内容。

基础部署会生成指定中英文 locale、启用本地 Docker 服务并加入 docker 组。通过 Bash 语法和登录 PATH 检查后，设置目标用户的默认 Shell 为 `/usr/bin/bash`。新登录后核验组权限及命令入口。保留现有 Docker 数据，不开放 TCP API。已有 npm pnpm 只有在旧锁、receipt 和实际身份一致时才退出原 owner；精确旧归档作为私有恢复材料保留，官方安装、后置验证及新锁提交成功后才退出。pnpm 迁移中断使用同一事务的 `recover --resolve completed` 续接，或 `recover --resolve restored` 从已核验的旧归档恢复原 owner 和锁；未知系统包归属会阻断撤回，不手写归属。

`repair base-environment` 可补回缺失的自有配置；用户修改继续阻断，缺失软件须回到有完整升级确认的 apply。`uninstall base-environment --confirm base-environment` 精确恢复原配置及适用原生设置，保留软件、文档和共享依赖；Docker 已有容器或镜像时保留服务和组设置。工具自身卸载不删除稳定基础配置。Python ABI 变化时通过同版本 bootstrap 重建私有工具环境，切换或回执失败保留原活动身份。

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

来源已使用 Lifecycle 中央库时，选择精确字段，由可信 SSH 子进程私有管道传送：

```sh
pennix secrets check --json
pennix secrets import-ssh --alias home-wsl-via-nas --field codex.api_key --field siyuan.api_token
pennix secrets check --json
```

中央库仍是私有明文 TOML。`check` 只返回当前配置所需字段及缺失项，包括保留的 Codex provider 已使用本工具 auth.command 的情况。`--field` 与旧 `--service` 来源互斥；接收端核验版本、精确字段集合、重复键、上限和配置未变后一次合并，不覆盖不同的非空值。失败保留原值。源端 `secrets export --private-stdout --field …` 仅供原生受控子进程使用，拒绝终端；**不得把该命令的输出交给 Agent、日志或任务记录**。SSH 使用已有信任，不自动接受主机密钥。OAuth、SSH 私钥、会话、数据库及 Paseo 身份不经此入口复制。

新增的 `PENNIX_COMPONENT_COPILOT_CLI` 默认关闭，旧配置省略时也保持关闭，保留既有外部 Copilot 的两项接入选择。全新设备可显式选择它，通过 AUR 安装 `github-copilot-cli-bin`，随后完成独立原生认证。原生模板检查等待依赖安装，但用户禁用 Hook、外来配置和漂移仍阻断。relay 首装等待 Paseo 原生配置生成独立 server-id 后才登记远端，不复制身份文件。

`PENNIX_TARGET_CCH_TMUX` 是独立、默认关闭的状态栏选择，依赖 tmux 和 CCH 连接。
连接配置不自动激活状态栏；明确选择后，由 CCH 原生 runtime lifecycle 管理运行副本和
`~/.tmux.conf` 中的自有段，保留连接秘密和外部内容。首次接管已有原生副本须显式允许
adopt，计划冻结现场摘要；后续 CCH 包 update/repair 会在同一事务刷新副本。文件或
可执行权限漂移、未知归属、待恢复事务均阻断，不用重装覆盖。恢复只接受同一 operation
的原生证明；此入口不即时 source 活动 tmux server。

`workflow.config` 是带格式主版本的 Kconfiglib 配置文件。`config edit` 和无 TTY
的配置文件路径使用同一个模型；`plan` 只读，`apply` 才改变组件。一次性
`uninstall <component> --confirm <component>` 不修改配置；要永久取消组件，先在
`config edit` 中取消选择，再执行 `apply`。

文档阅读可选择 `PENNIX_DOCUMENT_READING` 分组：MarkItDown 的四项 extras 和固定附加 Python 库共用一个 uv tool 环境，配合 Pandoc、LibreOffice、Poppler、中英文 Tesseract 数据及 Noto CJK 字体。各依赖仍可独立选择；默认配置不自动开启此组。现有外部系统包只验证原生包名、来源及可执行文件归属，保持 external，不写接管 receipt，也不因取消选择而卸载。首次缺包遵守完整系统升级门禁。

`package.extras` 是 uv extra 名称数组，`package.with` 只接受 `name==version` 固定需求，`package.imports` 映射所需模块和发行包名。安装、升级与恢复都从同一 catalog 生成参数；状态检查核对 uv receipt、入口归属、实际导入及固定版本。`package.identity_only=true` 仅用于无可执行入口的官方系统包；`external_satisfies=true` 仅允许已核验的外部官方依赖保持原归属。`resolve_dependencies=true` 允许 Pandoc、LibreOffice 和字体在已确认完整系统升级的边界内，使用 pacman 对固定签名本地包作原生依赖预览和安装；共享间接依赖由 pacman 管理，不伪造单独 Lifecycle 归属，也不在回退时递归删除。普通 npm/uv 包和有原生配置的组件不能使用这些系统包策略。新增组件还须登记 APPLY_ORDER、候选解析器和 Kconfig/defconfig，现有闭合集合测试负责发现遗漏。

固定 AUR 候选只在同目标、同事务已核验完整系统升级时，通过原生 `makepkg --syncdeps`
补齐声明的依赖。普通 repair 或单组件 update 没有该边界时不自动补依赖。缺失 AUR
包的首装中断可用 `recover --resolve completed` 沿冻结候选续接，须保有同次完整升级
证据，并在实际安装、精确身份及归属验证后才能核验步骤；包缺失本身不释放 unknown。

用户 Skill 集合保留 `pennix-skills` 和 `pennix-skills-extra` 层级，统一安装到
`$HOME/.agents/skills`，同一用户的宿主配置共用这份内容；CODEX_HOME 继续管理
配置与认证，不选择集合版本。旧 CODEX_HOME/skills 集合由 `pennix repair <collection>`
在原锁定提交迁移，`pennix update <collection>` 单独升级来源。plan/status 显示
`migration-required` 时先 repair；未知归属、内容漂移和新旧目标冲突拒绝覆盖。
迁移中断保留私有恢复副本及 journal，由 `recover --resolve completed` 核验并续接。
取消一个配置中的集合选择保留共享资源；显式卸载集合会影响此用户的全部宿主配置。
Context Mode 的三个命令 Skill 由其原生计划渲染，Lifecycle 按新旧回执核验和迁移，
卸载单个宿主接入时保留可能仍由其他宿主使用的共享 Skill。

Copilot CLI 接入必须显式选择 `PENNIX_TARGET_COPILOT_CONTEXT_MODE` 或
`PENNIX_TARGET_COPILOT_CODEGRAPH`，两项默认关闭；`PENNIX_COPILOT_HOME` 决定实际配置根。
`PENNIX_CONTEXT_MODE_CODEX` 保留既有 Codex 默认选择，程序安装可与宿主接入分别管理。
CodeGraph 从原生 global installer 取得 MCP 和 Codex 分层路由 Hook 定义，不创建索引。
Codex 的两个 handler 由 Lifecycle 通过真实 `hooks/list` 核验身份和当前摘要后配置确切信任，
保留其他 Hook 的顺序与信任；更新迁移旧 MCP 回执，失败按原字节撤回，漂移拒绝覆盖。
项目仍须由宿主信任且有本树独立可用索引；不自动初始化、同步或重建图。
Context Mode 使用固定包的
`setup --print copilot-cli --json` 计划，核验四类 Hook、三个命令 Skill 和 MCP 后再执行；
不复用 Codex TOML、Hook 信任或认证。共享命令包含各宿主的原生工具名，私有命令仍由原生发现。

Copilot 回执和目标登记按宿主、组件、实际目录区分，历史 `native/{key}.json` 只属于 Codex。
共享集合缺失时通过 `copilot skill add <目录>` 登记，记录新增项并精确撤回，不复制集合。
plan/status/check、组件 update/repair、取消选择后的 apply 与 recover 共用原有锁和事务。
repair 只恢复缺失的自有内容；未知编辑、回执损坏、用户禁用或管理策略禁用均保留现场。
失败的 configure 可沿原生 recover 恢复已记录前值，未完成的撤回只继续核验精确移除；
不调用 Context Mode 全局 remove，也不覆盖其他 MCP、用户 Hook 或已有目录登记。

与 `workflow.config` 同目录的 `components.lock.json` 记录每个动态组件已核验的
来源、版本、提交或发布引用和摘要。它由 Lifecycle 原子写入，不应手工编辑。首次
安装缺少锁时，`apply` 只解析一次当前候选，完成安装和后置核验后写入锁；已有可信
receipt 会先回填锁。已有安装缺少可信 receipt 时，`apply` 会阻断并要求先完成归属
迁移。普通 `apply` 按锁执行，不自动升级；明确升级使用
`pennix update <component>`，该命令先展示精确候选和影响，再在同一次交互确认后
执行单组件替换。

Arch/AUR 在外部升级后，使用 `pennix reconcile <component>` 显式接受当前精确身份。
它核对既有 receipt、旧锁、包名和可执行文件 owner，只同步记录，不安装、升级或降级。
`outdated` 保留兼容字段，表示实际身份与锁不相等，不代表实际版本更旧；输出另列实际
身份、锁定身份及既有受管记录。

Ponytail 只接受原上游的公开正式发行。Lifecycle 生成专用的本机 Marketplace 清单，
以完整提交固定 `ponytail@ponytail`；安装仍由 Codex 执行。`status` 分别显示登记、
实际载荷、锁和正式发行标签。同名标签改变、归档摘要冲突、共享或未知登记、用户禁用
均停止；不使用 `reconcile` 接受未发行载荷，也不以相同版本号判定安装成功。

旧受管 Git Marketplace 必须先执行 `pennix update ponytail-plugin --json`，审阅旧登记，
实际旧载荷、正式候选和 `review_sha256`。确认时使用
`pennix update ponytail-plugin --confirm <review_sha256>`；普通 `apply`、`repair`
和 `update --all` 不隐式迁移。中断后用 `pennix recover --json` 查事务，再传入
同一 `--transaction`、`--step` 与 `--resolve completed`（续接）或
`--resolve restored`（精确回退）。找不到已验证的前后值时保留现场。Lifecycle 不改
Hook 信任；需要复审时由使用者在 Codex 原生界面处理。

npm 组件可用 `pennix update trellis-cli --version 0.7.1-beta.0` 选择精确版本，仍由
登记的 registry 校验版本、包名和完整性，并沿同一次确认、事务、锁及 receipt 完成
替换；不修改组件的默认通道。此参数不接受 tag 或版本范围，也不适用于非 npm 组件。
恢复未完成的 update 时，指定版本必须与事务已确认目标一致；省略参数仍恢复原目标。

当前外部 Copilot CLI 的单项接管：`pennix update copilot-cli --takeover --json`。
只支持经过审查的 HomeWSL AUR `1.0.114-1 → 1.0.116-1`；预览核验两个固定提交的载荷、
现役文件、单包事务及 Hook。使用原摘要加 `--confirm` 才替换，替换通过后才记录回执和锁。
遇未知结果先用 `pennix recover --json` 核验；相同摘要仅能在已证明目标载荷全部实装时
完成记录，不重放包管理事务。已核验退回工件留在本机私有状态目录。

#### Paseo 插件

`pennix paseo-plugins` 管理 Chat Polish、Phone notify 和 Paseo 会话指标。设置
`CONFIG_PENNIX_PASEO_PLUGIN_PROFILE` 为共享声明的绝对路径后，以同一份 JSON 声明
在每台主机运行 `plan` 和 `check`。可从已审核的基线主机用
`pennix paseo-plugins capture` 提取声明。声明仅保存已审核的 Git 提交、插件启用状态和
受管的主机级设置。组件锁、本机回执、配对、凭据和事务仍由各台分别保存。

写入命令先预览并返回确认摘要，再用 `--confirm <摘要>` 执行同一操作。已有插件使用
`adopt <插件 ID>`。Chat Polish 的注册表来源切换会先记录原设置，再以同一提交原生复装、
恢复设置并校验。Phone notify 的同源同提交可原位接管。新安装和已声明设置变更使用
`apply`，外部漂移用 `repair <插件 ID>`。修改声明中的启用状态可暂停或恢复插件。

通用 `pennix updates` 在 `paseo_plugins` 中展示登记 GitHub 源的稳定发行 tag、精确提交、
已装提交和声明目标。`pennix update --all` 显示同一信息，Git 插件仍沿单项确认执行。目标由操作者写入共享
声明，使用 `pennix update <插件 ID> --json` 预览，随后用 `--confirm <摘要>` 逐台执行。
原来的 `pennix paseo-plugins update` 入口等价。候选和回退提交均从登记 Git 源取得，
核验原生插件 ID 及受管设置实现，使用 Paseo 原生预览和更新。设置实现变化时需要重新验证
迁移和回退。Paseo CLI 升级会对已配置插件声明预检公开插件 SDK。
`reconcile <插件 ID>` 只接受已严格达到共同声明的外部版本变化。单个插件暂停时，
由已核验的本机设置文档提供读取，
恢复后用 Paseo 原生设置接口校验。设置写入均走原生接口和 revision 校验。

从共享声明移除某项后，可用 `detach <插件 ID>` 释放归属并保留安装。
`uninstall <插件 ID>` 需要当时仍有归属证据，Paseo 会同时删除该插件设置。
使用 `pennix recover --json` 看未完成事务，用
`pennix paseo-plugins recover --confirm <事务 ID>` 按已记录状态继续。
命令作用范围限定在已登记的 Git 插件。

会话指标插件的 ID 为 `paseo-session-insights`，声明中的 `settings` 为 `{}`。接入 CCHP 时，在配置菜单的 Paseo 选项中启用会话指标 CCHP 取数，并填写控制面 HTTPS 源地址和推理密钥 ID。通过 `pennix secrets edit` 在私有 `secrets.toml` 的 `[cchp]` 中填写 `usage_token`。令牌需要目标组织的 `usage:read` 权限。切换令牌后，符合目标 `PENNIX_PASEO_HOME` 的插件服务端会在下一次配置刷新中取得新值。

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

Paseo 官方 Skills 使用候选 CLI 自带的公开 SDK，按实际导出、方法和操作结果核验，不使用版本白名单，也不要求 CLI、client、server 或 daemon 恰好同版。CLI 候选仍须与冻结载荷身份一致；Skills 清单取自该原生 server 包实际随附的 `dist/server/skills` 载荷，不固定名称数量。CLI 与运行中 daemon 版本不同时，实际 Skills 状态调用成功后才报告兼容。首装自动调用原生安装接口，覆盖 `.agents/skills`、`.claude/skills` 及原生自有的旧 `.codex/skills`；原本停止的 daemon 仅为该动作临时启动，并按同一实例停止。新 CLI 候选先在隔离前缀暂存、检查公开接口，再改变用户安装。安装或启停结果未知时保留已提交字段与事务，不恢复旧前台启动造成潜在双实例。远端 relay 登记和本地 daemon relay 配置分别记归属；register=n 仅表示不访问远端，不表示本地配置已撤回。

共享组件维护预检通过 Paseo CLI 自带公开 SDK 逐页读取原生 agent 标签和状态，返回完整扫描的计数及未关闭委派判定，不依赖可选 Trellis adapter。普通用户会话和历史数量不构成全局阻断；未关闭 Trellis 委派、身份改变、坏页、分页循环及核验失败仍阻断。配置中缺省为 n 的可选组件保持关闭；缺失可能启用新能力的选择仍须核对，不能静默新增安装。

Context Mode 通过 `@pennixrv/context-mode` 包、Codex 原生 MCP 和四个 Hook 接入；不叠加 plugin，也不改写用户 AGENTS。旧 FastCtx 的撤回由旧安装的原生入口完成，新配置格式 5 不接受旧组件键或旧格式。

Codex Context Mode MCP 通过原生 `env_vars` 动态继承两个 user bus 变量及标准 XDG 用户目录变量，未设置时保持缺省；不转发任意秘密，也不固化主机的 UID 或总线地址。CODEX_HOME 与 PATH 继续显式绑定受管目标。Codex 和 Copilot 的已有受管 Context Mode 接入均沿用回执中的 PATH，不因检查进程环境不同而提示配置更新；首次配置及缺少 PATH 的旧回执迁移须从已核验的原生环境执行。执行器的 shell、TTY、工作目录和预加载隔离仍遵守 Context Mode 合同，不能据 doctor 通过就认为它与交互式终端完全等价。

AoE 的 Codex Hook 由 Lifecycle 按归属管理。NAS 与其他主机上的服务端由各自的原生入口维护。CCH 使用经过摘要校验的私有 GitHub Release npm 包，需要现有 `gh` 认证。

从 0.3.41 起，Peon Ping 已退役。已选用的主机先在 0.3.40 以 `pennix uninstall peon-ping --confirm peon-ping` 撤回原生接入和运行文件，再关闭旧的组件与 Hook 选项，最后升级工具。0.3.41 可读取仍含关闭状态的旧配置。原生卸载保留音效包、偏好和相邻数据，其他宿主的外部接入由其各自 owner 撤回。

正确的既有包可通过 `PENNIX_ADOPT_EXISTING_PACKAGES` 显式纳管，接管前核对原生包名、版本和命令归属；更新须使用 `pennix update <component>`。对无 receipt 的客户端既有文件，先审阅 `plan` 的摘要，再用 `repair <component> --expected-sha256 <摘要>` 精确迁移，未知旁侧文件保留。

`check` 将本地安装/配置结果与功能请求分开输出。`PENNIX_FUNCTIONAL_CHECKS` 默认关闭；明确开启后才运行已选连接的原生功能入口，Windsurf 仅使用临时合成项目。失败、超时或没有入口均不能通过，不回显响应正文。SiYuan 的实际 MCP 会话连接仍由宿主原生工具核验，不能用本地 receipt 代替。

日常维护先运行只读 `pennix updates --json`，分别查看实际安装、锁定身份、登记来源候选、配置接入与重启影响，以及全部 Arch 系统包的可更新清单。Arch 查询复用官方 checkupdates 的临时数据库原则，使用 pacman-conf、fakeroot 和 pacman，核对真实同步库及已安装包前后不变；不写真实同步库。缺少能力或来源查询失败显示 blocked/unknown，不当作最新。固定集合仍跟踪 catalog 中的固定提交；允许保持外部归属的官方依赖单独标记 external。

`pennix-system-devel-skills` 默认关闭；选择它同时启用宿主工具要求。先用 `pennix plan --scope system-devel-tools` 审阅完整工具基线及缺项。缺包时用 `pennix apply --scope system-devel-tools` 进入仅限官方包的原生审阅；Arch 补包可能需要完整系统升级，实际事务不同就拒绝。工具已齐或未选集合时，该作用域不升级系统、不安装或接管已有包。`pennix check --scope system-devel-tools` 检查包和实际 PATH，不代替代表操作测试。

工具就绪后，首装用 `pennix plan --component pennix-system-devel-skills --json` 和 `pennix apply --component pennix-system-devel-skills`，升级用 `pennix update pennix-system-devel-skills`。这些定向入口不隐式执行系统升级、服务或其他组件动作；锁、归属或事务不符时停止。取消选择不删除共享集合或已装的系统包；显式卸载集合使用 `pennix uninstall pennix-system-devel-skills --confirm pennix-system-devel-skills`，也不撤回共享系统包。新建 Codex 会话检查共享集合发现与工具实际操作，不需要执行 Paseo 官方 Skills 安装。

需要更新时使用 `pennix update --all`，在真实终端统一审阅并确认。原生执行复用单一 journal 冻结候选、配置、锁、中央秘密摘要和 catalog，确认期间变化即停止。官方包必须处于完整系统升级边界内；清单包含非工作流包。实际系统包集合超出清单、配置漂移或副作用 unknown 时保留事务并停止，不自动接管或盲目重放。仅对同一事务已证实完成的系统升级，可用 `pennix update --all --review-system-changes` 展示额外实际差异并明确重新接受，原审阅仍保留；该入口不释放 unknown。已证实步骤续跑时跳过。`pennix update <component>` 保持原有单组件行为；`reconcile` 仍负责接受可证明的同源外部更新。

工具本身使用 `pennix tool update` 从固定公开发行发现、审阅和升级；核验固定版本载荷、公开 SHA-256、布局和身份后切换。切换结束后须在**新进程**重新运行 `pennix updates --json`；旧进程不继续执行内存中的组件 catalog，不后台自动更新。工具升级成功后只保留当前发行。完整、归属和权限安全的旧安装目录可清除；未知文件、符号链接和漂移会阻断。AGENTS 的正常升级要求归属凭据中的摘要仍匹配；人工审阅的迁移可用 `pennix repair codex-agents --expected-sha256 <完整文件摘要>` 精确确认当前字节，摘要不符不会写入。

已有 Codex 会话绑定 provider ID，切换网关时应保留该身份；由用户直接维护的既有 provider 选择 PRESERVE。若旧受管 provider 字段已人工恢复首次基线，可在审阅完整配置后使用 `pennix repair codex-config --expected-sha256 <完整文件摘要>` 释放不再选择的归属。未知漂移、仍被选择的漂移字段、缺少可信记录或摘要不符仍拒绝。普通 apply/inspect/uninstall 不放宽漂移保护。已移除的 remote_compaction_v2 feature 不再生成，旧 Kconfig 选项仅作兼容输入；原生配置迁移只清除可证明归属的旧 feature，保留 provider 的 capabilities.remote_compaction。

发行 tag 和资产不可覆盖。同版本失败重试只复用摘要完全匹配的已暂存发行；工具卸载保留其他 CLI 仍需的 PATH、共享 linger、用户数据、组件归属及未结事务元数据。`tests/test_release_payload.py` 以实际构建载荷验证隔离 bootstrap、入口恢复和重装；Arch 包、下载使用明确替身，不代表真实新机、跨登录、PAM 或所有外部服务已经实测。
