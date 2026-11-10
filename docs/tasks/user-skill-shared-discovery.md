# 用户 Skill 共享发现入口

根任务：`codex-workflow-optimization/.trellis/tasks/in_progress/11-09-user-skill-placement-governance`。实施依据为已批准的方案及 `implementation-authorization.md`，由当前会话直接实施。本组件基线为 `2fc01de3ddf68a5ca12143c90d90ebfee966b689`。

集合内容保留层级，目标改为 `$HOME/.agents/skills/<collection>`；旧 CODEX_HOME 安装须由原生 repair 在同一锁定提交迁移，update 单独升级来源。校验 receipt、内容、来源和命令归属，目标冲突或漂移拒绝覆盖。复用用户级锁、事务 journal 和 recover，恢复材料在扫描根之外；取消配置不隐式卸载共享集合。显式卸载说明影响全部宿主配置。

实施涉及集合 owner、CLI 的 plan/status/repair/recover、既有测试、用户规则模板和发布 catalog。按领域测试、完整 unittest、发行构建及正式发布链验证；真实部署、行为验收和根 gitlink 由根任务协调。失败保留可信恢复材料和 journal，不手改安装树。正常停止于本组件正式发行、原生安装及根验收闭环。

2026-11-10：完整单元 199 项通过（2 项需显式运行环境而跳过），其中原生 repair journal → recover、复制中断、目标冲突、来源升级阻断、取消选择仍保留均已覆盖。Context Mode schema 2 → 3 迁移、回执失败回滚及未知共享文件拒绝通过；Skills 来源固定为 `cdf12907706ca615f4f4c184cd9d0db3490a7682`。发行版本 `0.2.46`；Context Mode 沿既有 npm beta 来源和锁文件固定机制更新到 `2.1.5-pennix.2`，不引入第二份版本白名单。正式 payload 门禁、实际部署和组合证据由根任务补录。
