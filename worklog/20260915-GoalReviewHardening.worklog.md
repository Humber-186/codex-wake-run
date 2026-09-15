# Goal Review 可靠性收口工作日志

## 2026-09-15

- 完整阅读 `/tmp/review.md`，目标范围确认为少量可信 Linux 用户和 Codex CLI 0.154.0+。
- 采纳 orphan 窗口、共享 orphan release、Goal 锁瞬时竞争、paused lease 丢失四项正确性问题。
- holder 只增加 `prepared/spawning/running` 三阶段；completion 已持久化与 wake 已投递继续由 completion v2 表达，不复制状态源。
- release 的 lease ID 不匹配保留 `skipped_conflict`：旧 completion 不应重试并干扰同一线程的新 lease；启动阶段的 lease 缺失或 ID 不匹配则严格失败。
- 本机确认为 `codex-cli 0.154.0`；官方 App Server Goal API 未提供原子 CAS，也没有向 skill 暴露当前 TUI live runtime endpoint。独立 stdio App Server 的当前 turn usage 记账限制将显式呈现，不伪造 live accounting。
- 不引入共享 daemon、跨版本兼容矩阵、全量真实 Codex CI 或第二套 completion 状态机。
- holder lease schema 升级为 v2，持久化 `prepared/spawning/running` 与可诊断的 `target_pid`；v1 `target_started` 在读取时做最小升级，避免升级过程中破坏已有 paused lease。
- worker 在 `Popen` 前写入 `spawning`，成功后写入 `running`；写入失败会清理已启动的整棵目标进程树，lease 丢失会在 `Popen` 前失败。
- release 与 join 会扫描全部 holder；发现 dead worker、无 completion 且处于 `spawning/running` 时，将整个 lease 持久化为 `orphaned` 并保持 Goal paused。
- paused lease 缺失不再报告 `restored`；completion 的 Goal release 保持 `retrying` 并记录显式错误。旧 completion 遇到不同 lease ID 仍安全终结为 `skipped_conflict`。
- Goal lease 改用阻塞式 OS advisory lock，delivery lock 继续非阻塞，避免正常短暂竞争改变原有 replay busy 语义。
- `goal_guard` 对 paused 模式新增 `runtime_scope: detached` 与 `current_turn_accounting: not_guaranteed`；README 与 Skill 同步说明当前 turn usage 和公开 API 无 CAS 的边界。
- 稳定支持范围明确收敛为 Linux + Codex CLI 0.154.0+；Windows 路径保留但不纳入当前可靠性承诺。
- 新增启动标记顺序、Popen 前 lease 丢失、Popen 后标记失败清理、持久 orphan、共享 holder 阻断、缺失 lease retry、阻塞锁和 v1 lease 升级测试。
- 首轮完整回归达到 93 项；新增行为通过。一次文件长度失败准确暴露 `wake_run_goal.py` 为 501 行，已通过将 App Server Goal adapter 移至其协议客户端模块并压缩签名解决，没有放宽 500 行门禁。
- 修正持久 `orphaned` lease 的 replay 归属判断顺序：仅关联当前 `log_dir` 的 holder 才在该次 replay 中报告，避免全局 lease root 下无关任务污染结果。
- cancellation 在已知 paused lease ID 时对 lease 丢失显式报错；未取得 guard 的 gate/launcher 失败仍允许 `not_needed`，避免混淆两种状态。
- 最终执行 `PYTHONWARNINGS=error` 下的语法检查与完整单元测试：共 93 项，90 项通过，3 项 Windows 条件测试按预期 skipped。
- Skill `quick_validate.py`、500 行文件上限、100 行函数上限与 `git diff --check` 均通过；`wake_run_goal.py` 最终 499 行。
- 使用本机真实 `codex-cli 0.154.0` 通过独立 stdio App Server 读取当前线程 Goal，确认移动后的 RPC adapter 工作正常。
