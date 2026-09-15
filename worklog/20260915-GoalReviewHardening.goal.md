# Goal Review 可靠性收口

## 预期目标

批判性吸收 `/tmp/review.md`，在少量可信 Linux 用户、Codex CLI 0.154.0+ 的明确范围内修复 Goal Guard 的真实可靠性缺口，同时避免引入 daemon、兼容矩阵或重复状态机。

## 验收标准

- worker 死在目标 `Popen` 前后时，lease 均不会被误判为可安全恢复。
- 任一共享 holder 已 orphan 时，其他 holder 不得恢复 Goal，新 watcher 不得加入。
- paused guard 的 lease 在目标启动前丢失时明确失败，目标进程不启动。
- Goal lease 的短暂并发锁竞争不会立即导致 watcher 或 release 失败。
- 独立 App Server 的当前 turn Goal usage 记账限制在启动结果与文档中显式可见。
- 自动化测试覆盖上述路径，代码指标和现有测试保持通过。

## 行进状态

- 状态：已完成
- review 已逐项判定；采纳的控制流修复、边界说明、测试和文档均已完成。

## 最终已交付成果

- Goal holder 使用 `prepared/spawning/running` 三阶段，关闭 `Popen` 前后的错误恢复窗口，并记录目标 PID 供诊断。
- orphan holder 会将共享 lease 持久化为 `orphaned`，阻止其他 holder 恢复 Goal，也阻止新 watcher 加入。
- paused lease 在目标启动前丢失会阻止 `Popen`；完成后的 lease 丢失保持 Goal release `retrying`，不再假报 `restored`。
- Goal lease 使用阻塞式 OS lock；delivery lock 保持原有非阻塞语义。
- paused Goal Guard 明确返回 detached runtime 与当前 turn accounting 不保证；文档同时说明公开 Goal API 无 CAS 的窄竞态边界。
- 稳定支持范围收敛为少量可信 Linux 用户和 Codex CLI 0.154.0+，未引入共享 daemon、版本矩阵或重复 completion 状态机。
- 新增 Goal lease schema/helper 与专项测试；v1 holder 状态可最小升级。
- 最终验证：共 93 项测试，90 项通过、3 项 Windows 条件测试跳过；warning-as-error、Python 编译、Skill validator、文件/函数上限与 diff 检查通过；真实 Codex 0.154.0 App Server 只读 RPC 通过。
