# Pennix Lifecycle

`pennix` 管理 Pennix 工作流的用户级配置和已登记组件。安装器本身位于
`~/.local/share/pennix-lifecycle/releases/<version>`，入口位于 `~/.local/bin/pennix`。

bootstrap 是独立 Bash 入口：它准备 Arch 基础依赖，从固定 GitHub Release 下载运行包并校验 SHA-256；无需 clone 仓库、Codex 或已安装的 Skill。普通用户执行：

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://github.com/PennixRv/pennix-lifecycle/releases/latest/download/bootstrap.sh | bash -s -- install
pennix config init
pennix config edit
pennix plan
pennix apply
```

可用 `--release vMAJOR.MINOR.PATCH` 固定版本；root 调用必须显式传入 `--user USER`，工具自身卸载使用 `bootstrap.sh uninstall`。bootstrap 只准备工具运行依赖，不安装 AUR helper 或工作流组件。

新机可直接使用仓库内的公开 WSL 默认配置生成主机配置；主机专属 URL、ID 和路径
在生成后编辑，秘密仍单独填写：

```sh
pennix config init --defconfig /path/to/pennix-lifecycle/config/defconfig-wsl
pennix secrets edit
```

已有可信 SSH alias 时，也可以按服务选择性导入固定 owner 凭据文件。该快捷方式只
读取登记的凭据字段，不复制认证文件、URL、ID 或路径：

```sh
pennix secrets import-ssh --alias office-wsl-via-nas --service codex
```

`workflow.config` 是带格式主版本的 Kconfiglib 配置文件。`config edit` 和无 TTY
的配置文件路径使用同一个模型；`plan` 只读，`apply` 才改变组件。一次性
`uninstall <component> --confirm <component>` 不修改配置；要永久取消组件，先在
`config edit` 中取消选择，再执行 `apply`。

秘密位于 `secrets.toml`，由 `pennix secrets edit` 打开并以明文编辑，文件权限为
0600；其中只保存凭据字段，普通 URL、ID 和路径属于 `workflow.config`。JSON 输出
不会包含秘密值。
