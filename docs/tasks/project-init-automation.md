# 新项目初始化策略与发行

根任务：codex-workflow-optimization 的 project-init-trust-automation，已由用户明确要求“现在开始推进到停止点吧”，根 Task CLI 路线 revision 2 已接受完整合同并授权实现、正式发行及两机部署。组件沿 main；本仓使用既有 docs/tasks 记录，不新建第二套任务运行时。

本组件修改两项默认关闭的 Kconfig 策略、受限只读 config policy 查询及测试、必要的已验收来源 pin 和发行版本。查询只解析声明，不读取秘密、探测包锁或执行系统／项目操作。项目实际初始化、索引和信任分别交 Trellis、CodeGraph、Codex 原生入口。

验收包括旧配置兼容、独立选择、未知及非法配置拒绝、无副作用及无秘密输出；运行完整适用 unittest、构建和公开载荷测试。组件正式发行后交根任务，经原生 Lifecycle 在 home-wsl／pennix-wsl 部署，核对声明、实际载荷、新进程和无未结事务，再由根任务完成集成归档。

源码验证完成：341 项 unittest 通过、4 skipped（其中实际归档测试在构建后单独执行）；bash 语法、Python 编译和 diff 检查通过。公开 defconfig 明确两项策略为 n，旧声明无需自动启用。集合来源固定到已验证并推送的 7937a1bc48f489c7214501e9b3141114b55d105a。发行候选 0.3.34，远端 tag 未占用；后续对本地及公开归档分别验证实际载荷，不以此处源码通过代替正式发行。
