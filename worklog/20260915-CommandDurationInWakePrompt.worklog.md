# 在唤醒系统提示中加入命令耗时工作日志

## 2026-09-15

- 定位唤醒消息构造、worker 执行结果和 completion 持久化链路。
- 使用 `time.monotonic()` 和子进程资源统计记录每次目标命令的 wall/user/sys 耗时。
- 对经济看护重试累计各次执行的 wall/user/sys 指标，并写入 completion 顶层与尝试明细。
- 在唤醒消息中加入可用的 `wall`、`user` 和 `sys`，旧事件缺失的指标不生成对应字段。
- 更新 `SKILL.md`、中英文 README 和消息/集成测试。
- 执行 `PYTHONWARNINGS=error timeout 90s python3 -m unittest discover -s tests -v`：63 项通过，3 项 Windows 专属测试跳过。
- 审核修复：Windows 不再把 PowerShell 宿主进程耗时作为整条命令的 CPU 耗时，省略不可可靠取得的 `user/sys` 字段。
- 审核修复：完成事件持久化失败时，通知使用包含全部重试尝试的累计 wall/user/sys 指标。
- 恢复完整唤醒消息契约断言，并增加指标计算、不可用指标和累计故障通知测试。
- 审核修复后执行 `PYTHONWARNINGS=error timeout 90s python3 -m unittest discover -s tests -v`：67 项通过，3 项 Windows 专属测试跳过。
