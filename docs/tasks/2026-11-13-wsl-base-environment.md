# WSL开发基础环境与配置交付

## 授权与基线

根任务pennix-wsl-base-environment已接受实施合同并取得用户“批准完整推进”的授权。组件基线0c1b4da（0.2.58），原现场位于fix/existing-project-spec-convergence。用户指出须遵守既有分支与发布规则后，已核验历史明确约定及.gitmodules：组件在既有main工作树维护，不新建分支或工作树。main已无损快进到相同基线，所有未提交改动逐文件核验字节与权限不变；本轮误建的feat/wsl-base-environment已删除，未推送。根任务保有整体决策和验收，本文件记录组件实现和验证，不建立第二套任务状态机。

发布沿用既有正式 tag CI，验证固定源码、公开载荷及实际安装身份；不覆盖或移动已发布 tag。原方案中“建立功能分支”的安排被用户本次明确纠正覆盖，不作为执行依据。随后已完成全仓与子模块同步审计；用户“批准完整推进到所有工作完成和git收敛”，恢复当前实施合同，不新建分支或工作树。

## 变更边界

当前缺口是工具引导依赖无人维护AUR、没有正式基础环境适配器及配置投递合同、PATH只覆盖.zshrc，以及pnpm来源与目标不符。行为归属在bootstrap/release owner、配置模型/CLI、基础适配器、path_env、共享catalog/tmux owner及正式发布链。

修改范围：bootstrap.sh及纯标准库安装入口/发行运行环境；config/Kconfig与profile兼容；scripts/pennix.py、_engine.py和现有事务分派；专用base_environment及references清单；templates内审查后的P10k/Shell/编辑器/Git公共配置；path_env和pnpm/文档Python现有owner；对应tests、README和发布文件。复用owned_files、锁、lock/receipt/journal，不新增通用模板框架、配置同步器或事务系统。不自动部署全部工作流组件，不修改SSH/NAS/Windows拓扑，不复制用户秘密和运行数据。

W1 隔离实现及回归，W2 正式发布与公开载荷身份，W3 新机 pennix，W4 存量 home，W5 根集成、清理及思源文档。依次执行；以下验证记录区分源码、发行载荷与真实设备。

## 验证记录

- 修改前完整隔离unittest：228项，19.253秒，OK，2项原有skip。
- 后续定向/完整回归、公开载荷及设备验收结果随实施追加。

## W1 实现

新增显式 development profile 和 base scope；复用现有组件 owner、锁、receipt 与 journal，交付稳定基础配置和私有哈希锁定工具环境。基础包使用官方仓库及经过维护状态检查的 AUR 包；Node.js 24 LTS、官方 pnpm、系统 Rust/Cargo 和 Go 使用统一 PATH。旧 npm pnpm 迁移保留精确旧归档，支持同事务续接或恢复。配置接管绑定审阅过的摘要，保护用户修改和外部内容。

P10k 从已审阅的 home 配置提取全部 439 行有效代码及必要作用域、函数和自定位逻辑，剔除原注释体和缓存，不复制认证或机器身份。原文件 SHA-256 为 `65e00e42e4e9e95e6ff80b16248b7e8be1af582f36aae37fa47b79c0aadb69b1`；保留上游 MIT 许可。

Node.js 24.22.3 的官方 Arch 包已通过签名核验，并在临时目录运行现役主要 CLI；Paseo 与委派适配器的六个 Linux 原生模块均可加载。官方 pnpm 12.19.0 在隔离项目中识别并使用 packageManager 指定的 pnpm 10.32.1。两项预检均未修改真实系统包和项目锁。
