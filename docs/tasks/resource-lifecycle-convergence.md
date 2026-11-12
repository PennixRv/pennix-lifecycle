# 资源生命周期收敛集合固定与发行

本组件继承根任务 `workflow-resource-lifecycle-convergence` 已接受合同和用户“现在完整推进到任务停止点”的实施、发布与部署授权。修改目标仅为集合来源 pin、发行版本及本记录；不新建全局 GC、不改事务和安装协议、不执行完整系统升级。

源码基线为 `ae6e753529a2ad918d3f4562a2a66a160c7f5960`，版本从 `0.2.56` 升为 `0.2.57`。catalog 将 `pennix-skills` 固定为 `5761d9ff00d73dd6f50083c3dd5acbfbe3865a38`，包含原生无损交接 ZIP、历史只读、精确摘要确认和失败恢复，以及资源 owner 清理指引。其他组件 pin 保持。

验证使用完整隔离 unittest、`bash -n bootstrap.sh`、`git diff --check`、固定提交的 `scripts/build-release.sh` 和独立实际 payload 测试。公开发行只由既有 tag CI 生成；根侧核验公共 archive/checksum/版本/pin 与固定源码一致后，通过该包的 `pennix tool install --json` 原生更新，再用 `pennix update pennix-skills --json` 切换固定集合。旧发行退出继续复用已实现的归属、权限、摘要与恢复检查，不增加第二套清理器。

0.2.57 的公开发行与原生自安装、集合 update/repair 已完成。随后真实历史交接检查发现封存额外请求完整任务阶段投影，导致旧任务分类无效时无法取得其有效 ownership 事实。集合提交 `07a378eee3cac1b007d5851c79870d0e2aa002ac` 移除该额外要求，并验证 absent 可继续、claimed 和未知事实仍保护。0.2.58 仅升级此固定集合来源；不覆写已发布版本，不改变其他组件 pin。相同隔离测试、实际 payload、公开 checksum、逐成员源码比对和原生更新流程再次适用。

最终发行提交 `908464cae72e0b70cbd1d31688d6345dc638316a`、tag `v0.2.58`，CI `43204396451` 成功。隔离源码测试 228 项通过（2 项原生默认跳过），实际载荷隔离测试另行执行并通过。正式资产 SHA-256 为 `45d18332a17338d28975d8bcc974f2877b56adc997aafdaa13568a65e3d3e5bc`，与公开 checksum 一致；39 个受约束成员的完整解压 tar 与固定提交本地构建逐字相等。gzip 容器字节可因运行环境不同而变化，未据此声称压缩产物完全可复现。

已从该核验正式包执行原生 tool install，当前入口为 releases/0.2.58/pennix，旧自有发行已由 owner 退出，releases 仅保留 0.2.58；随后新 catalog 的 Skills update 与同版本 repair 均 changed，6 个关键文件与源码相等。Trellis CLI/adapter 精确 .19 更新和 repair、三项目资产更新、根 Node 24.16.0 full-local/offline/handoff 已通过；recover idle，cache list 为空。Codex、Pandoc、AOE 的既有外部包身份漂移仅记录，未纳入升级或 reconcile。Lifecycle 交付范围完成，真实历史处置仍由根任务按精确批准推进。

本记录建立时仍在验证；源码候选、版本字符串和安装成功均不代表整体任务完成。真实历史迁移、删除、远端配置和服务中断不在本组件批准范围，仍由根任务的具体对象预览和原生 owner 合同决定。
