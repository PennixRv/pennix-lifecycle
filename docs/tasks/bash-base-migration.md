# Bash 基础环境硬切

根任务为 `pennix-lifecycle-bash-migration`，已接受实施合同，路线版本为 `1`。本仓库在既有 `main` 维护基础环境源码、测试、文档与正式发行。起点 `5aa1045b087825cf0d01fbe942ee0c1ae829689e` 与已登记远端维护分支一致。

本次修正基础环境强制 Zsh、PATH-only 模式只写 `.zshrc`、旧受管启动文件的已知漂移阻断 Bash 硬切三处行为。修改范围是基础适配器及 PATH 管理、基础包和 Bash 模板、Yazi 的 catalog 与 Kconfig 登记、生命周期输出和相应测试。纯系统服务、用户工具前缀、运行时 owner 及库函数由现有组件继续负责。

正式发行后只在 Office WSL 写入。保护现有工程、用户配置与数据。使用真实启动器核验 PATH、提示符和相关组件，切换后用新 SSH 连接核对 Bash。根仓负责记录集成验收和已接受的 gitlink。

源码回归共 348 项，344 项通过，4 项按既有条件跳过。原生 AUR 配方构建了固定提交的 ble.sh 0.4 开发版包并核验 fzf 集成文件。隔离 tmux 中，受管 Bash 配置通过 fzf 历史回放、文件选择、目录切换、Tab 补全与 Starship 失败状态验证。
