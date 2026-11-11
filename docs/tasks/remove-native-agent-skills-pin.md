# 原生子代理路径移除的集合固定

本组件继承根任务 `11-11-codex-native-subagent-policy-violations` 的源码、测试、发布部署授权，仅承担必要来源固定和原生发行，不另建方案。集合候选 `823c3b69049d421e39d0768031669914e33f384a` 已通过 manifest 验证、集合 15 项及路由 7 项测试，并已推送远端分支。原生目录当前固定旧提交，必须通过 catalog pin 才能部署该候选。

本次仅修改 `pennix-skills` pin，Lifecycle 版本由 `0.2.53` 更新为 `0.2.54`。保留其他组件、安装器、权限、配置、秘密与事务协议。沿既有完整隔离 unittest、原生 build-release、独立 payload 测试和 GitHub CI 发行；公共归档核验后通过其自身 `pennix tool install` 安装，再原生更新集合。禁止手改安装树、锁或 receipt。
