# 工作流体检 W04：生命周期整改与最终来源固定

用户本轮明确“批准”根任务workflow-health-remediation-release-deploy的完整合同cafeaf12543e5aa07154ed5416439f58f1016bc64ce522c90be7421bf64899fa，实际来源为根approval.md。沿main，不新分支／工作树，不写入委派；当前仅实施源码与隔离回归，正式发行和真实两端部署按根W08—W11。

范围为HA01/04/05/06/07/25/29/30/31/32/34：固定Trellis同版beta包组；CodeGraph平台可选包完整性与受管缺包拒绝；base AUR固定配方而非yay重新求解，VCS不豁免；基础撤回及工具自升级的持久恢复；MCP有效禁用／过滤准确表达且保留用户策略；共享Hook位置trust保护；Relay远端身份／资源归属、锁内读写及真实启用顺序；Tavily专项探针显式provider。复用已存在候选、文件归属、锁、journal和原生owner，不增加第二套任务系统。

定向回归覆盖首次／重复／修复／撤回、SIGKILL持久边界、同版本不同配方、缺平台包／错误摘要、他方Hook两顺序／外部Stop、禁用与工具过滤、alias换目标／旧receipt／锁竞争／nginx失败及Tavily失败而Direct成功。完整Python／shell及发行载荷检查之后才提交推送和发布。最终catalog等待全部受测正式组件来源固定，不先用开发树安装。

必要失败停止依赖动作，unknown沿原生恢复不重放或手造receipt；不替用户启用连接、重授trust、复制OAuth／身份／用户数据。真实预算沿根合同0/12起算，无自动重试。最终停止点仍为本地先行、NAS实际Relay部署、PennixLaptop同一发行原生重装及完整验收。

源码验收：完整Python 312项通过（启用真实Paseo隔离入口；仅发行载荷条件跳过），包含真实离线npm shim＋平台包安装、缺包显式修复、主包漂移拒绝和原生卸载；新增非法回执标量／平台元数据负例。Paseo使用真实CLI、临时home和本地监听完成首次／重复安装、停止态恢复及Relay配置往返，不发送模型请求。受管CodeGraph旧版只绑定shim的锁不能自动变成新组合摘要；正式升级到新固定版本后建立组合锁，不放宽旧摘要。

候选工具版本0.3.22，集合固定b82f21882173cc291c62f8cfcdb7d569dc462249；extra及Hikari保持原验收来源。组件包仍由原生候选冻结、npm完整性和锁核验，不在catalog维护第二份动态版本表。真实工具重装复用bootstrap原生uninstall后同一固定发行install，保留配置、秘密及组件记录；同版本无动作不算重装。发行载荷必须从已提交候选构建并经过隔离bootstrap／失败恢复／卸载重装门禁后，才允许打tag发布。
