# Goal 模式兼容

## 预期目标

让 wake-run 在 Codex Goal 激活时能够真正进入事件驱动等待，不被自动 Goal continuation 提前唤醒；后台任务完成后先可靠投递完成事件，再安全恢复由 wake-run 暂停的 Goal。

## 验收标准

- active Goal 启动 wake-run 时，Goal 被持久化租约保护并暂停，且启动响应明确报告已验证的保护状态。
- Goal 保护失败时不启动目标命令、不返回虚假的 `armed`。
- 完成事件先持久化并成功 queue，之后才有条件恢复 Goal。
- 用户原本暂停或在等待期间修改、清除、完成 Goal 时，wake-run 不覆盖用户状态。
- 同一 thread 的并发 watcher 共享 Goal 租约，任一完成事件成功投递后可幂等释放。
- pending completion 与 Goal lease 均可恢复，错误显式暴露。
- 自动化测试覆盖关键状态机、冲突与失败恢复路径；现有行为无回归。
- SKILL.md 与用户文档准确说明 Goal Guard 行为和限制。

## 行进状态

- 状态：已完成
- Codex goal：交付已验收，本轮结束前标记 complete
- 调研结论：采用 App Server Goal API、持久化 per-thread lease、两阶段启动、queue-first release；不使用 `/goal resume` 文本、SQLite 直写或静默降级。
- 实现、自动化验证以及 active Goal + idle + 真实 queue 的最终 release gate 均已通过。

## 最终已交付成果

- 新增持久 App Server JSONL 客户端与 Goal RPC 适配层。
- 新增 per-thread 持久 Goal lease，支持并发 watcher 共享、严格快照校验、幂等释放与显式冲突结果。
- 启动链路改为 worker prepared、Goal Guard acquire/verify、commit gate、target running，保护失败时目标进程不会启动。
- 完成链路改为 completion persist、wake queue、Goal conditional release，投递与恢复状态可分别重放。
- 状态锁改用操作系统 advisory lock，并实现启动前遗弃 lease 恢复和启动后 orphan fail-closed。
- 新增 `--goal-policy=auto|require|ignore`，默认 `auto`；失败不静默降级。
- 更新 SKILL.md、英文 README、中文 README 与完整调研存档。
- 通过 84 项自动化测试、3 项平台条件跳过、语法检查、代码指标检查、skill validator 和 diff whitespace 检查。
- 通过真实 Codex 0.154.0 active Goal 往返测试、完整 launcher/worker/completion 测试及真实 idle/queue 唤醒 release gate。
