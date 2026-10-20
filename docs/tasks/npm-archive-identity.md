# 标准 npm 归档身份校验

根协调任务：codex-workflow-optimization/.trellis/tasks/10-20-audit-residual-convergence。基线 main 37b9c422、0.2.26。用户已批准将阻断 CCH 安装的这一处前置修复纳入当前任务。本仓库未初始化 Trellis，不创建第二套任务状态。

## 问题与范围

CCH v0.1.48 正式 npm tgz 有唯一的 package/package.json，GitHub digest、独立 SHA256SUMS 和编译文件均已核验。_archive_json 仅匹配 '/package/package.json' 后缀，错误拒绝路径精确相等的标准 npm 归档。原生 update 在候选阶段退出，未启动安装。

仅修改 scripts/candidates.py 的路径精确或目录边界后缀匹配，保留唯一身份、大小、JSON 类型、包名/版本和摘要检查。回归追加 tests/test_candidates.py，覆盖标准路径、前缀路径、缺失/重复身份及错误包身份/摘要。不修改 CCH 制品、不手改缓存、安装副本、lock、receipt 或 journal，不扩展其他审查点。

## 验证与交付

先加入失败回归，再修复；执行完整 unittest、bash -n bootstrap.sh、git diff --check 与原生 scripts/build-release.sh。候选 patch 为未占用的 0.2.27，通过 VERSION 文件及 main/tag/既有 CI 发行，核验正式 tar.gz、checksum、GitHub digest 和包内内容。当前本地回归已覆盖标准路径并通过，正式发布与安装仍待完成。

根任务使用经核验的正式 bootstrap，固定 v0.2.27 安装工具，由原生 release owner 切换受管入口及工具 receipt；只清除该 owner 能证明安全的旧工具发行，漂移则阻断。确认配置、组件锁、已有 receipt 和 journal 状态后，重新运行原生 pennix update cch-status，候选须与已验收 v0.1.48 的 ref/digest 一致。无全量 apply、配置迁移或其他升级。

遵守根任务唯一完成停止点：正式制品、工具安装、CCH 原生更新、项目资产、实际宿主、根组合门禁均须完成；本维护记录及代码/版本/最终证据全部提交推送 main，最终 HEAD 与实时 origin/main 相同、ahead/behind=0/0。必要现场重连时保留未完成断点，不将源码或发行通过视为整体完成。
