# Paseo CLI 0.12.0 lifecycle 固定与发行

根任务：`codex-workflow-optimization/.trellis/tasks/10-19-paseo-daemon-0120-update`。该组件任务只负责 lifecycle 源码 pin 与正式发行；本机安装和 daemon 验收继续由根任务执行。

## 修改范围

- 将 `references/catalog.json` 中 `paseo-cli.approved_version` 从 `0.11.4` 更新为 `0.12.0`。
- 将 lifecycle `VERSION` 从 `0.2.18` 更新为 `0.2.19`。
- 保持包名、npm registry、安装 prefix、所有者及现有 `allow_scripts` 不变；不在本组件任务实现版本提醒，也不改用户运行配置。

## 基线与依据

- 基线为 `main` 提交 `c8cd6f84e9664bbc986abe3409dc16c440317eb9`，开始时工作树干净且与 `origin/main` 一致。
- 官方 npm `@getpaseo/cli@0.12.0` 的 `latest` 与 `beta` dist-tag 均指向 `0.12.0`；npm 完整性为 `sha512-cR3Ywux/nbgscGvWbaRtOgB+5HkDwHcS3BtcLRn1IXZ7xKcWpbwHxIFhyOsD+d4c8BNvB/l/7lEv20wZuu5hUQ==`。
- `0.11.4` 与 `0.12.0` CLI 的依赖名称一致，三个 Paseo 内部包同步从 `0.11.4` 到 `0.12.0`；CLI 与这三个包均未声明 `engines` 或 `preinstall`/`install`/`postinstall` 脚本。当前 Node 为 `v26.11.1`，既有生命周期脚本许可 `esbuild`、`node-pty`、`msgpackr-extract` 保持原样。
- [官方 v0.12.0 发布说明](https://github.com/getpaseo/paseo/releases/tag/v0.12.0) 提及 `paseo run --background` 改用 `--no-wait`，以及 permit 确认超时错误码变化；该说明没有描述 daemon 配置或持久 agent 数据迁移。

## 验证、发行与停止点

- 运行发行工作流对应的完整 `python -m unittest discover -s tests -v`、`bash -n bootstrap.sh`、`git diff --check`、catalog JSON 解析，以及 `scripts/build-release.sh` 发行包构建。
- 提交并推送组件 `main` 和 `v0.2.19` 标签；核验发行 CI、GitHub Release 资产 SHA-256、固定源码提交与正式获取包一致性。
- 若源码验证、CI、正式发行或摘要核验失败，不向根任务交付固定提交，也不安装本版本。
- 本组件停止于已验收并推送的 lifecycle `0.2.19` 源码提交及正式发行物；宿主安装、Paseo daemon 停止/启动和运行态验收归根任务。

## 实际交付

- 当前源码差异仅含 catalog pin 与 `VERSION` 两处；完整 unittest 71 项通过，`bash -n bootstrap.sh`、catalog JSON 解析和 `git diff --check` 均通过。
- 组件提交、发行包构建、远端 CI/Release 和正式资产摘要仍待完成；未通过这些门禁前不向根任务交付固定来源。
