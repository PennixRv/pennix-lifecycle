# Pennix 生命周期完整收敛

关联根任务 `.trellis/tasks/in_progress/11-07-converge-pennix-lifecycle`；根已批准合同 revision `24db3ff9e7d997bf2ffcdefb7006894006d980df3382e76b5339d42f16d2e00a`，用户明确“批准，现在开始吧”。本源码 owner 无 Trellis 初始化，沿已有 docs/tasks 记录，不初始化项目。

本仓 main 基线 `6aae5ca7e0f2cf0c42fc0b92c01963fedbf68620`。范围为显式精确 reconcile（Arch/AUR 与 Codex 插件）、准确失配/PATH 诊断、Windsurf owner 输入兼容，以及 Paseo 仅自启合同。复用 package_identity、原生插件 API、owned_files、apply_journal 和既有服务 receipt，不引入新依赖或第二套事务系统。

变更需抵达 CLI/parser、engine 身份核验、journal/resume、connection_owners、Kconfig/defconfig、Paseo plan/apply/check/observe/restart/update、回归和文档。确认后重验配置、锁、receipt 与候选；未知所有权、并发变化、活动工作或未知副作用阻断。Paseo 独立健康 daemon 不停止；新 unit 为 oneshot 原生 daemon start，不持续控制；移除入口不停止 daemon。普通 apply 不复活已停止实例。

顺序：代码及回归 → Windsurf 发行、集合固定可消费 → catalog pin 与本仓完整测试/发行 → 原生安装 → 精确 reconcile、Windsurf 配置、Paseo 迁移 → 根侧全集验收与清理。运行 unittest discover、test_bootstrap.sh、发行包检查；隔离 user unit 与真实宿主验证由根任务记录，不注销或重启 WSL。

唯一正常停止点为源码提交推送、原生发行与安装、全部已选 check、Windsurf 一次真实搜索、Paseo 自启合同验收及本任务备份清理、原生收尾完成。任一候选漂移或验证失败先保留证据并恢复，不能把部分成功写成完成。源码以新提交恢复；文件只经 owner 原子恢复；不编辑安装缓存、锁或凭据来掩盖错误。

## 当前实施结果

已完成本仓源码、CLI、Kconfig/defconfig、catalog 动作声明、诊断和调用方修正。141 项 unittest、bootstrap 参数及隔离检查、git diff --check 均通过。回归包含实际 reconcile handler 的精确确认、输入漂移、receipt/lock 部分失败与恢复；插件原生精确接口；Paseo 停止与自启分离、旧入口迁移、活动保护和部分 stop 失败恢复。真实主机只读计划确认 AoE、Codex CLI、uv 的既有受管记录和锁/实际身份分别显示，PATH 为 keep。

隔离真实 Paseo 0.15.2 与 systemd user target 已验证启动器退出后存活、重复触发 PID 不变、原生 stop 不复活、原生 restart 保留 supervisor 并替换 worker、关闭/移除入口不杀 daemon，测试进程与入口已清理；真实用户 daemon PID 532274 保持健康。实际跨登录未测，也未操作真实服务入口。

Windsurf v0.1.17 已在原始 tag 和 tarball 上直接发布；流水线 attempt 3 的 publish 成功，立即下载因传播期 404 导致 run 整体失败。公开可见后，独立下载摘要、精确 npm 安装、registry signature 与 attestation 均通过。集合沿原生 npm-pack 物化 evidence E 50147de6a46bad1c02f2868915fbd77da8d3b061，19 文件字节与权限一致，verify、12 项集合测试、9 项评估通过并提交推送。catalog 不额外发明集合 pin；既有原生 resolver 在消费时固定集合完整提交与摘要到 lock。

远端未占用的 Lifecycle 版本为 0.2.33，准备沿原生 tag workflow 验证、构建和发行。主机安装、reconcile、Windsurf owner 导入、真实自启迁移及根全集验收仍待执行，不能把源码检查等同于交付完成。未改动的 Grok 固定依赖有 npm audit high 基线，根验收记录独立风险，不在本仓越权修改生成依赖。

## 发布与主机后置条件

v0.2.33 固定源码 cb5f260c723a01b9636c555701dddda6e9e749d7，原生发行 job 42359008396 通过，公开包 SHA-256 为 08f099eedd19355f6c16949ec956819eb8ae28eacbe9fa6290ba5f09ca9622c4。bootstrap 原生下载校验并安装 0.2.33，只保留当前完整发行；公开包与源码和安装中的全部 29 个运行文件逐字节相符。本机构建压缩包摘要不同，不能宣称跨平台压缩字节可复现；公开 checksum、固定源码和运行文件身份已分别核验。

集合消费 c607488ec6ef7f0855bd79d15c303beda159f43e。原生 reconcile 已分别确认并接受 codex-cli 0.167.0-1、uv 0.13.7-1、aoe 1.22.1-1；没有安装、升级或降级这些包。Ponytail 原生 Marketplace 对齐现有 5.3.3/ref 96608e078b2d5ea607c5c3b8b5a54d1c1fba6846，后置核验通过。一次误用非登记简称 ponytail 被 CLI 拒绝，无副作用，随后使用完整 ID ponytail-plugin。

通过原生配置模型启用 Windsurf owner 模式、RUN=n，保持功能批量探针和系统升级关闭。apply 仅改变 Windsurf 连接和 Paseo 自启 unit，Paseo 配置与 PATH no-op。22 登记组件（21 已选，1 未选运行态记录）及 6 已选连接的 check/status 均 exit 0。Windsurf 来源 owner-config，0700/0600、Devin 原样，单次公开合成项目真实搜索 complete/exit 0。Paseo unit 为 enabled oneshot、daemon start、RemainAfterExit=yes、Restart=no、KillMode=process；真实受控 start 后健康 daemon PID 532274 与 worker 均不变。真实跨登录未执行，沿已批准的隔离 target 触发及真实受控触发替代。根侧组合验收、清理及原生收尾详见关联根任务。
