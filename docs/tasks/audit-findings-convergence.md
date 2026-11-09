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
