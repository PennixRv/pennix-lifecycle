# 更新归属、验收与组件遗留

根侧已授权实施合同 `6f809de3…` 的 L1–L4 工作包。维护分支 `main`；起点 `6645a742` 已对照远端，不新建分支。

- `_engine.py` 的插件查询与同市场保护：所有调用走已登记 Marketplace 的原生清单，不从全量超时或缓存推断身份。
- `relay_owner.py`：在管理入口区分可靠回执边界与超时，保持实际授权、登记与运行进程不变。
- `pennix.py` 及现有候选、journal：Copilot CLI 仅单项受控纳管，不能隐式接管或签发空操作回执。
- `paseo_plugins.py` 与原生 JS 适配：仅支持经核验的 Chat Polish 旧、新设置合同及转换，不能绕过精确设置、版本或恢复检验。实装范围仅为 HomeWSL。

验证使用 `tests/test_lifecycle_state.py`、`test_relay_identity.py`、`test_paseo_plugins.py`、候选和事务测试及原生发行验收。遇未知实机身份或不能退回时，不写实际宿主；保持同一事务核验，不临时放宽确认。
