# 架构审查发现整体整改

## 目标和批准

落实根任务 `lifecycle-audit-findings-convergence` 已展示且获用户“批准并完整推进到全部完成”的 WP-L。需求、逐项处置、完整合同和批准证据保存在根任务 `.trellis/tasks/in_progress/11-09-lifecycle-audit-findings-convergence/`，本仓未初始化 Trellis，不自动初始化。基线 `90ad9a9fabaf515cd2a6841477a2e3f59e671f91`，实际分支 main，upstream origin/main；变更前核对远端维护 tip。

## 范围

L1 修复身份/目标互斥、配置快照与确认、来源/锁/平台和事务恢复；L2 消费原生 owner 协议，修复 Paseo Skills 首装、依赖撤回、共享资源和旧 receipt；L3 完成隔离、真实发行载荷 smoke、文档和发行一致性。涉及 scripts、config、references、tests、bootstrap 和原生 release workflow；各文件在具体修改前读取调用方。只提取已有共同事实与窄 helper，不新建通用编排框架。

19 对象、1 state、6 连接及合成资源均受根合同覆盖。协议 provider 先交付，再启用消费者；普通 apply 不追最新；unknown 不清记录；不跨 owner 自动回滚，不扩 OS/CPU/shell、多 profile 或离线重建。

## 验证和交付

先审隔离，再运行相关 unittest/bootstrap 回归，最终原生完整测试和实际构建载荷 smoke；记录源码、载荷与安装入口身份。正式发布后经原生 bootstrap/Lifecycle 部署到本机，核验目标、版本、receipt/lock、实际命令和事务终态；根组合验收及 Git 收尾后停止。真实双平台新机/跨登录及未触达外部服务不宣称已验证。归属、隔离、门禁、发行身份或恢复失败停止依赖动作并保留证据。

## 发布候选

版本 0.2.39，集合固定为 `886ebf1a1b6f413f366ec9cfa92cc4e0391772d8`；包含 Grok 原生提交 `896b81edc1c0a5ad02edf9a313eaeda97f147ed0` 及 Windsurf v0.1.18 精确公开 tarball 投影。Windsurf 公共包 SHA-256 为 `30e15719cc69406bdb33fa595fcb4fc96ebf164ce74029f3796a49b01e6a3f54`，原生 registry 安装、签名和 provenance 验证通过。CCH v0.1.51 原生 GitHub Release 载荷 SHA-256 为 `3d11b98a2d6544be3a48f6d4255f1fd862b04a62d92feacb12638aa32fe06272`，本机已通过原生 update 升级。

最新 Lifecycle 隔离全量 177 项（包含真实 Paseo 公开 SDK 合同）通过，仅未选定实际发行载荷的测试跳过；随后针对实际构建的 0.2.39 载荷 smoke 1 项通过，覆盖 bootstrap、非默认 CODEX_HOME、失败恢复/同版本重试和卸载重装。最终 tag 构建仍须复测同一发行载荷，CI 上传前同样执行隔离载荷门禁。新机、跨登录和未触达远端服务保持未验证。

逐项交付复核继续补齐 M04/M05/M08/M30/M39：默认 HOME 意图可移植、XDG 空值回默认且拒绝相对值、pacman official/foreign 身份核对、catalog 动作有限集合及 state 的不可执行声明、移除未消费 branch。Syu 后清除候选缓存，并刷新有归属的取消选择 official 包供后续安全撤回，不刷新 AUR。卸载补同一平台门禁。新增反例和完整回归进入 0.2.40。

v0.2.39 的 CI 在真实载荷门禁失败，未发布；Ubuntu 的 sudo 系统别名指向隔离 `/etc` 外的目标，测试未复制其别名链。夹具现仅重建 curl/sudo 的必要 `/etc` 别名，替身绑定到最终命令目标，继续隔离用户目录、网络与服务。旧 tag 保留，不覆盖发行物；以 v0.2.40 重新完整验证和发布。

v0.2.40 完整 183 项、CI 和公开载荷 smoke 通过，原生 tool install 与集合 update 成功；随后原生 apply/check 六连接全部 current/keep/owned、事务 idle、功能检查 not_requested。实际公开包 `6d0c42150e8fc6842b17913c97c21e8ec676a518f8654d46c7cfd592cf01a578` 与本机构建压缩字节不同，但全部运行文件字节/权限一致，原生 runtime digest 与 receipt 一致。逐分支最后复核发现取消 relay 的闭包在赋值前读取局部 paseo 变量，已调整解析顺序并通过真实 cmd_apply 入口隔离反例；v0.2.41 再经完整测试、实际载荷门禁、正式发布及本机升级，不以 v0.2.40 的本机通过代替最终版本验收。

## 最终交付

v0.2.41 最终 source/tag 提交 `09e0f21f5f7578810eb8f16e769bf0e4bdcf52e3`；包括 SSH 导入 alias 接线修正，以及首次秘密写入的私有目录和非 UTF-8 脱敏反例。隔离完整 186 项全部通过，显式包含实际 Paseo 公开 SDK 和实际构建载荷，bootstrap 参数/目标隔离检查通过。原生 CI 42568168434 完成源码、上传前载荷门禁及正式发布。

正式 GitHub tar.gz SHA-256 为 `9e3f45380a60a11b7540a73655c5646fe02764da3e4e520e4ca9fe15196eda54`，与公开校验和一致；全部运行文件内容/权限与本机构建一致，下载的公开包独立 smoke 通过。经公开包原生 tool install 本机升级 0.2.41，公开解包/实装/receipt 的 runtime digest 均为 `b05e6fca9f164850e1617d74c1b8e5dc66e785497062efe58dc24ccb6a3726fe`。原生 check 退出 0、六连接 current、functional_checks=not_requested；recover idle。没有重启现有 daemon、触达未选择的付费检索或执行真实系统包升级。

F01–F22、M01–M42 的全部处置及验证逐项回写根任务 verification.md；A01/A16 原失败未改写。D08 允许的真实双平台新机、跨登录、PAM/runuser、全部外部服务及 Codex 插件磁盘旅程保持未验证，不由隔离替身推定通过。此后仅归档文档与根组合收尾，不改变本次发布运行资产。
