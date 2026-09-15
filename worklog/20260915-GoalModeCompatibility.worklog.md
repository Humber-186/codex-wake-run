# Goal 模式兼容工作日志

## 2026-09-15

- 创建 Codex goal：解决 wake-run 与 Goal 模式不兼容，并完成设计、实现和验证。
- 完整读取 `skill-creator` 指引、项目 `AGENTS.md`、现有 `SKILL.md` 与 `/tmp/goal-mode.md` 调研。
- 确认根因：active Goal 在线程 idle 后自动 continuation，与 wake-run 的“armed 后立即结束 turn，等待 `codex queue`”契约冲突。
- 确认实施方向：公开 App Server Goal API；持久化、共享、可验证 Goal lease；supervisor prepared/commit gate；completion persist → queue → conditional Goal release；失败显式暴露。
- 已开始审查现有 launcher、worker、状态持久化与恢复测试，准备确定最小可靠改动面。
- 核对 OpenAI 官方 App Server 文档与本机 Codex CLI 0.154.0 生成 schema，确认 Goal API 字段、状态枚举与“省略 objective 保留用量”的更新语义。
- 实现持久 App Server JSONL 客户端；只读真实 RPC 已成功读取当前线程 active Goal。
- 将启动流程拆成 supervisor `prepared` → Goal Guard acquire/verify → commit gate → target `running`，Goal 保护失败不会启动目标。
- 实现全局 per-thread Goal lease、共享 holder、严格 snapshot 冲突检查、queue-first release、幂等 restore 与显式 `skipped_conflict`。
- completion schema 升级为 v2，分别持久化 wake delivery 与 Goal release；保留 v1 completion 的显式重放兼容。
- 将跨进程状态锁替换为 POSIX `flock` / Windows `msvcrt.locking`，避免 PID 复用和陈旧锁删除竞态。
- replay 可恢复 target 启动前遗弃的 lease；target 已启动但 completion 缺失时标记 orphaned 并保持 Goal paused，防止假成功。
- 更新 SKILL.md、英文 README、中文 README 的 Goal Guard 契约和 CLI policy 说明。
- 真实 Codex CLI 0.154.0 Goal RPC 往返通过：active → verified paused → restored active，objective 保持一致。
- 使用转发真实 App Server、截获 queue 的测试 wrapper 跑通完整 launcher/worker/completion：`armed.goal_guard=paused/verified`、target exit 0、wake delivered、Goal restored、lease terminal restored；随后精确清理测试 wrapper 与终态 lease。
- 增加 App Server 持久 JSONL 会话测试、Goal lease/冲突/并发/release retry/recovery/orphan 测试；修复协议客户端资源关闭告警。
- prepared worker 不使用任意 gate 超时：它检查 launcher 进程存活，launcher 消失即显式取消，避免为了“能跑”增加隐藏边界。
- 自动化结果：84 tests passed，3 个平台条件测试 skipped；文件/函数指标、skill validator 与 `git diff --check` 通过。
- 最终真实 release gate 通过：run `fc7cf4cb5740` 在 active Goal 下进入 idle，3 秒期间没有 Goal continuation；后台任务退出 0 后，真实 `codex queue` 以 wake `5e464d838c9f465d93ba9966ff3de1fb` 唤醒本线程。
- 核验 completion v2：wake delivery 为 `delivered` 且仅尝试 1 次，Goal release 为 `restored` 且仅尝试 1 次；目标日志包含 `WAKE_RUN_GOAL_IDLE_E2E_OK`，当前 Goal 恢复 active，lease 进入 `restored` 终态。
- 精确清理该次真实验证产生的临时日志、completion、lock 与终态 lease；项目交付收口完成。
- 文档收口后执行最终回归：`py_compile`、84 项单元测试和 `git diff --check` 全部通过，3 项 Windows 平台条件测试按预期 skipped。
