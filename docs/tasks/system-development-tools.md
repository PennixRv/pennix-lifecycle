# 系统开发工具预设与 Skill 集合联动

根仓任务 `11-27-systems-skills-c-cpp-tooling`，实施合同 `c02ef778d9416`，维护分支 `main`，起点 `3ce5137`。本组件负责 Arch/WSL2 的只读要求核验、定向官方包事务和恢复；根仓负责跨仓库验收与 Git 对账，Skill 集合只分发文档。

`references/system-devel-tools.json` 维护完整要求。它不是 HomeWSL 的缺包列表，不接管已有软件包，也不改项目工具链。工具已经满足时不刷新系统或重装。集合定向首装和升级不隐式触发完整系统升级；缺包引导到独立作用域，软件包事务仍须由操作者审核和通过原生确认。

- 在与实际 Codex 一致的执行入口运行 `pennix check --scope system-devel-tools`，核对软件包、系统路径和 PATH 选择。它不发网络请求、不执行被项目覆盖的同名程序，也不宣称应用、内核或跟踪权限可用。
- `pennix plan --scope system-devel-tools` 在缺包时用私有临时同步库生成预览。只有经审阅的官方目标可进入 `pennix apply --scope system-devel-tools`；需要完整系统升级时，仍须在原生 `pacman` 最终提示核对实际清单。取消最终确认可能已经刷新同步库，不等于安装成功。工具已齐或未选择时不行动；包事务未知时沿同一 `pennix recover` 检查，不重放。
- Valgrind 在部分 Arch 构建上需要匹配动态加载器 Build ID 的调试符号。按受信项目或 Arch 提供方准备对应符号，再对已知程序实跑 Memcheck。`check` 不自动联网取得符号；无符号且启动失败时，区分程序入口已装和功能未验证。
- `perf` 和 `trace-cmd` 的离线工件读回与实时采集是不同的验证层。预设不修改 sysctl、capabilities、tracefs 或设备权限。
