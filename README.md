# Pennix Lifecycle

`pennix` 管理 Pennix 工作流的用户级配置和已登记组件。安装器本身位于
`~/.local/share/pennix-lifecycle/releases/<version>`，入口位于 `~/.local/bin/pennix`。

先执行基础环境准备，再执行工具命令：

```sh
./bootstrap.sh install
pennix config init --defconfig references/defconfig-wsl
pennix plan
pennix apply
```

`bootstrap.sh update` 只切换已发布的固定版本；`bootstrap.sh --uninstall` 只接受本工具写入的 owner marker。引导脚本不从当前目录推断安装目标，也不覆盖同版本 release 目录。

发行归档只打包运行时文件，不包含 `bootstrap.sh`。提交候选后可用以下命令生成确定性归档和 SHA-256；引导脚本内固定的摘要必须与最终标签生成的归档一致：

```sh
scripts/build-release.sh 0.1.7 <git-ref> /tmp/pennix-lifecycle-0.1.7.tar.gz
```

`workflow.config` 是带格式主版本的 Kconfiglib 配置文件。`config edit` 和无 TTY
的配置文件路径使用同一个模型；`plan` 只读，`apply` 才改变组件。一次性
`uninstall <component> --confirm <component>` 不修改配置；要永久取消组件，先在
`config edit` 中取消选择，再执行 `apply`。

秘密位于 `secrets.toml`，由 `pennix secrets edit` 打开并以明文编辑，文件权限为
0600。也可以使用 `pennix secrets set <field>`，或从明确指定的 SSH alias 导入固定字段：

```sh
pennix secrets import-ssh office-wsl-via-nas codex.api_key siyuan.api_token
pennix secrets check --json
```

JSON 输出不会包含秘密值。SiYuan、CCH、Hikari、Grok 和 Windsurf 的连接目标只由
对应原生 owner 写入；生命周期工具会先核对普通 endpoint、组件选择和秘密字段，缺少
原生动作时阻断 `apply`，不会伪造适配器。
