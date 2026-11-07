# Pennix 生命周期完整收敛

关联根任务 `.trellis/tasks/in_progress/11-07-converge-pennix-lifecycle`；根已批准合同 revision `24db3ff9e7d997bf2ffcdefb7006894006d980df3382e76b5339d42f16d2e00a`，用户明确“批准，现在开始吧”。本源码 owner 无 Trellis 初始化，沿已有 docs/tasks 记录，不初始化项目。

本仓 main 基线 `6aae5ca7e0f2cf0c42fc0b92c01963fedbf68620`。范围为显式精确 reconcile（Arch/AUR 与 Codex 插件）、准确失配/PATH 诊断、Windsurf owner 输入兼容，以及 Paseo 仅自启合同。复用 package_identity、原生插件 API、owned_files、apply_journal 和既有服务 receipt，不引入新依赖或第二套事务系统。

变更需抵达 CLI/parser、engine 身份核验、journal/resume、connection_owners、Kconfig/defconfig、Paseo plan/apply/check/observe/restart/update、回归和文档。确认后重验配置、锁、receipt 与候选；未知所有权、并发变化、活动工作或未知副作用阻断。Paseo 独立健康 daemon 不停止；新 unit 为 oneshot 原生 daemon start，不持续控制；移除入口不停止 daemon。普通 apply 不复活已停止实例。

顺序：代码及回归 → Windsurf 发行、集合固定可消费 → catalog pin 与本仓完整测试/发行 → 原生安装 → 精确 reconcile、Windsurf 配置、Paseo 迁移 → 根侧全集验收与清理。运行 unittest discover、test_bootstrap.sh、发行包检查；隔离 user unit 与真实宿主验证由根任务记录，不注销或重启 WSL。

唯一正常停止点为源码提交推送、原生发行与安装、全部已选 check、Windsurf 一次真实搜索、Paseo 自启合同验收及本任务备份清理、原生收尾完成。任一候选漂移或验证失败先保留证据并恢复，不能把部分成功写成完成。源码以新提交恢复；文件只经 owner 原子恢复；不编辑安装缓存、锁或凭据来掩盖错误。

## 当前实施结果

已完成本仓源码、CLI、Kconfig/defconfig、catalog 动作声明、诊断和调用方修正。141 项 unittest、bootstrap 参数及隔离检查、git diff --check 均通过。回归包含实际 reconcile handler 的精确确认、输入漂移、receipt/lock 部分失败与恢复；插件原生精确接口；Paseo 停止与自启分离、旧入口迁移、活动保护和部分 stop 失败恢复。真实主机只读计划确认 AoE、Codex CLI、uv 的既有受管记录和锁/实际身份分别显示，PATH 为 keep。

隔离真实 Paseo 0.15.2 与 systemd user target 已验证启动器退出后存活、重复触发 PID 不变、原生 stop 不复活、原生 restart 保留 supervisor 并替换 worker、关闭/移除入口不杀 daemon，测试进程与入口已清理；真实用户 daemon PID 532274 保持健康。实际跨登录未测，也未操作真实服务入口。

Windsurf v0.1.17 代码与证明已推送，发布流水线 42355980168 的 validate job 全部通过，但 publish job 在 npm whoami 返回 E401、未执行 publish；registry 仍明确 E404。按既定依赖顺序，集合 pin、Lifecycle 新版本分配/发行及本机变更保持待执行。本提交仅固定已验证的实现进度，不表示交付完成；无需因外部认证修复重新索取同范围批准。
