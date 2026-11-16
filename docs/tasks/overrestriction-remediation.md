# 升级过度限制收敛

本组件沿 main 实施根任务 `workflow-overrestriction-remediation` 已批准合同，合同摘要为 `5970565880f9176ffebc77cd8d04628aedb951cc299c44e4b93422b9cff7e956`，批准证据为根任务 `implementation-approval.md`。不新建分支或工作树，不写入委派。

修复范围：Paseo 按公开 SDK 和实际操作能力核验，去除具体版本白名单及无依据的版本相等耦合；维护预检通过 Paseo 自身 SDK 检查委派运行态，消除可选 Trellis adapter 的循环依赖；缺省为关闭的可选组件不再要求显式赋值。继续保留包身份、数据完整性、未知运行态、归属、配置类型、依赖和显式选择保护。复核官方 Skills 清单是否存在同类封闭假设。

先补定向反例，再运行完整源码和正式发行载荷检查。通过后提交、推送、发布新的不可变版本，并通过原生 bootstrap 在 Pennix 和 PennixLaptop 真实卸载、重装 Lifecycle。生产 Paseo 不升级、不重启，不执行模型或付费检索请求。配置、秘密、身份和用户数据保持原生 owner 管理；失败沿 journal/recover 处理，不手改状态。

## 部署暴露的解释器路径耦合

0.3.27 公开发行及载荷通过后，Pennix 真实卸载／安装检查发现 SiYuan MCP 的 header helper 期望值跟随当前 Lifecycle 私有虚拟环境解释器变化，原稳定配置因而被判为 update-required。进一步安装 0.3.28 后原生 plan 全部保持，证明此次无需修改现有 SiYuan 配置；不能把这次误判描述成已有连接或秘密丢失。helper 仅用 Python 标准库，改为当前 Arch 目标稳定的 `/usr/bin/python3`，不再随工具发行 PATH 改变。

新增实际子进程反例覆盖前置旧／新发行 Python 路径、失效旧解释器的受管配置迁移以及用户额外字段保留。0.3.27 保持不可变，最终候选提升 0.3.28，重新完成完整源码、公开载荷和双端原生重新部署；PennixLaptop 在源端最终全绿前保持 0.3.26。

## 最终验收

0.3.28完整324项通过、3条件跳过；本地构建与实际公开载荷分别通过bootstrap／失败恢复／卸载重装，正式CI43948416593 success。公开归档SHA256为97247c9e601e34a9afda1da7c3c04a7bfa16947f4c3a001f4fe3bb656376f04b，53个成员类型／权限／内容对应受测源码。

Pennix先行真实卸载0.3.27后安装0.3.28；源端检查通过才对PennixLaptop真实卸载0.3.26并安装同一0.3.28，均证明活动入口先缺失再落盘。两端随后通过原生update将Trellis包组更新beta.21、CodeGraph更新1.6.2-pennix.6，实际载荷摘要一致、动作均changed。

双端最终check exit0（源端31已选组件／9连接，目标端32／9）、recover idle，functional_checks_requested=false。7项保存基线（含原不存在项）逐端保持，生产Paseo PID／serverId／0.15.4版本均不变。目标端原有Pandoc实际为3.11-83、旧锁3.11-80，由原生reconcile在可信旧receipt与同源pacman身份核验后只接受已安装身份，未升级或降级系统包。根组合57passed、0failed／blocked，既有4覆盖边界如实保留。根任务承担最终gitlink、交付与原生归档；本组件无合同内未结项。
