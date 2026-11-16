# 工作流体检 W04：生命周期整改与最终来源固定

用户本轮明确“批准”根任务workflow-health-remediation-release-deploy的完整合同cafeaf12543e5aa07154ed5416439f58f1016bc64ce522c90be7421bf64899fa，实际来源为根approval.md。沿main，不新分支／工作树，不写入委派；当前仅实施源码与隔离回归，正式发行和真实两端部署按根W08—W11。

范围为HA01/04/05/06/07/25/29/30/31/32/34：固定Trellis同版beta包组；CodeGraph平台可选包完整性与受管缺包拒绝；base AUR固定配方而非yay重新求解，VCS不豁免；基础撤回及工具自升级的持久恢复；MCP有效禁用／过滤准确表达且保留用户策略；共享Hook位置trust保护；Relay远端身份／资源归属、锁内读写及真实启用顺序；Tavily专项探针显式provider。复用已存在候选、文件归属、锁、journal和原生owner，不增加第二套任务系统。

定向回归覆盖首次／重复／修复／撤回、SIGKILL持久边界、同版本不同配方、缺平台包／错误摘要、他方Hook两顺序／外部Stop、禁用与工具过滤、alias换目标／旧receipt／锁竞争／nginx失败及Tavily失败而Direct成功。完整Python／shell及发行载荷检查之后才提交推送和发布。最终catalog等待全部受测正式组件来源固定，不先用开发树安装。

必要失败停止依赖动作，unknown沿原生恢复不重放或手造receipt；不替用户启用连接、重授trust、复制OAuth／身份／用户数据。真实预算沿根合同0/12起算，无自动重试。最终停止点仍为本地先行、NAS实际Relay部署、PennixLaptop同一发行原生重装及完整验收。

源码验收：完整Python 312项通过（启用真实Paseo隔离入口；仅发行载荷条件跳过），包含真实离线npm shim＋平台包安装、缺包显式修复、主包漂移拒绝和原生卸载；新增非法回执标量／平台元数据负例。Paseo使用真实CLI、临时home和本地监听完成首次／重复安装、停止态恢复及Relay配置往返，不发送模型请求。受管CodeGraph旧版只绑定shim的锁不能自动变成新组合摘要；正式升级到新固定版本后建立组合锁，不放宽旧摘要。

候选工具版本0.3.22，集合固定b82f21882173cc291c62f8cfcdb7d569dc462249；extra及Hikari保持原验收来源。组件包仍由原生候选冻结、npm完整性和锁核验，不在catalog维护第二份动态版本表。真实工具重装复用bootstrap原生uninstall后同一固定发行install，保留配置、秘密及组件记录；同版本无动作不算重装。发行载荷必须从已提交候选构建并经过隔离bootstrap／失败恢复／卸载重装门禁后，才允许打tag发布。

## 本机升级补丁0.3.23

0.3.22正式发行及公开载荷隔离测试通过后，Pennix真实升级Context Mode暴露Copilot预检循环：新版完整doctor先要求旧Skill等于新版模板，Lifecycle因此无法刷新自己持有回执的旧Skill。包升级已verified，连接配置effect=none；原生recover按同一transaction／step证明not-started后结清，未手改状态。

修复只改Copilot Hook可用性查询：通过宿主公开`copilot --server --stdio --no-auto-update`的`hooks.discover`直接读取精确user hook文件的4事件和enabled，避免把模板刷新当作Hook开关预检。保留Lifecycle原有用户策略检查；未知／缺项／禁用／无效帧／超时／超限一律拒绝，查询子进程必定退出，不运行模型、不调用Hook、不重授trust。旧Skill仍须与可信receipt相符，外部漂移不因此放行。补充真实本地Copilot无认证临时home的metadata查询和显式禁用策略负例，另有分段协议／错响应／缺项／foreign事件回归。

0.3.22不可变发行保留；修复后以0.3.23重新走源码、正式载荷、CI发布和部署，最终工具版本以0.3.23为准。

## 受管CodeGraph旧安装迁移补丁0.3.24

0.3.23已在Pennix正式安装，Context Mode真实repair及Codex／Copilot接入成功。随后新守卫发现已安装CodeGraph1.6.2-pennix.4的两个正式平台目录均缺失，旧版本此前依赖缓存自恢复；新的拒绝执行符合HA04，但update也因此拒绝，旧锁仅绑定shim，不能用组合摘要repair假装旧锁有效。

补充受控升级：仅当可信receipt、旧lock、准确入口和主包元数据共同证明旧shim归属时，按旧版本取得原生registry完整性载荷，核对旧单包摘要或原组合摘要，再逐文件证明安装主包未漂移；确认后从新固定shim＋平台双载荷升级。引擎在真正写入前再次校验旧主包及旧锁绑定；不执行缺平台旧shim，不接管外部安装，不接受主包漂移，不读写或搬运旧缓存。正常包和新组合锁的守卫保持。

回归包括旧单包／组合摘要证明、另一个主包拒绝、确认前证明、外部归属拒绝、真实离线npm从缺平台的旧版升级到新版及原生卸载。0.3.24替代本轮最终工具候选，既有不可变发行均保留。
