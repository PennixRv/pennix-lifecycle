# 对齐会话续行修复的主 Skill 集合

根任务 `11-26-session-routing-behavior-convergence` 的发布工作包。Lifecycle 仅更新主集合的固定来源和发行版本；Trellis 原生任务及项目模板由 Trellis 维护。保留 `0.3.48` 已固定的系统开发集合以及现有 extra 来源。

- [ ] 核对主集合固定提交 `ad0de0304b4dac8d3c90b87fdb53b9ac4243572e` 与公开归档及本次验收来源。
- [ ] 验证 `0.3.49` 发行载荷与精确 tag；公开资产、摘要或原生归属不符则不在 HomeWSL 切换。
- [ ] 通过 Lifecycle 原生入口安装并核对主集合锁与回执；没有变更的集合不重装，不更新 Paseo daemon 或会触发其重启的 CLI。
