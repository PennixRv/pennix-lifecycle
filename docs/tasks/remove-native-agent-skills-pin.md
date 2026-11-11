# 原生子代理路径移除的集合固定

本组件继承根任务 `11-11-codex-native-subagent-policy-violations` 的源码、测试、发布部署授权，仅承担必要来源固定和原生发行，不另建方案。集合候选 `823c3b69049d421e39d0768031669914e33f384a` 已通过 manifest 验证、集合 15 项及路由 7 项测试，并已推送远端分支。原生目录当前固定旧提交，必须通过 catalog pin 才能部署该候选。

本次仅修改 `pennix-skills` pin，Lifecycle 版本由 `0.2.53` 更新为 `0.2.54`。保留其他组件、安装器、权限、配置、秘密与事务协议。沿既有完整隔离 unittest、原生 build-release、独立 payload 测试和 GitHub CI 发行；公共归档核验后通过其自身 `pennix tool install` 安装，再原生更新集合。禁止手改安装树、锁或 receipt。

本地完整隔离测试 228 项通过（2 项既有条件跳过），实际 payload 安装、恢复、重装测试通过。`v0.2.54` 的 CI 两次均因 Ubuntu 镜像 DNS 失败停在依赖准备，未发行、未部署。改为复用 source gate 已采用的 PyPI 路线，在临时 runner 的系统 Python 位置安装相同纯 Python 依赖；bubblewrap 与 yaml 仍须真实可用，否则失败。隔离测试和实际 payload 门禁保持。向前发行 `0.2.55`，不改动已推送 tag 或绕过 CI 发布。
