# 审阅可靠性修复工作日志

## 2026-09-16

- 对照审阅逐项检查 adopt、supervisor、stage scanner、completion、registry、control 与 Goal lease 实现。
- 确认前三项高优先级问题、runtime 频繁 fsync、退出码可用性误报和 control 超时假失败均真实存在。
- 决定采用小范围修复：唯一 attempt 握手 + holder rollback；统一阶段提交器；阶段短时投递；runtime 终态观察；明确 detach 排空；pending control 返回。
- 同一日志行采用“最多推进一个阶段”的确定语义；不扩展动态阶段策略。
- 新增 `wake_run_stage_commit.py`，让 owned/adopted observer 共用 durable event 提交和 checkpoint 节流逻辑。
- 阶段恢复从 event 文件恢复已完成前缀与最大 durable offset，覆盖“event 已写、runtime 未写”的崩溃窗口。
- 阶段 queue 使用默认 20 秒短投递策略；终态只等待当前活跃投递，未开始事件保留 pending，由 completion/wake 汇总并交给 replay，避免多个阶段串行累加等待。
- detach 明确排空已提交阶段事件，ack 携带 `stage_delivery`；control 前台等待超时返回 `pending`，不再把 durable 请求误报为失败。
- adopt 加入唯一 `attempt_id`、Run 级锁、prepared 前身份验证、gate 身份验证、Goal holder rollback 和 cleanup warning；gate 后目标退出落为 `observed_exit`。
- 修正 detached/orphaned/lost 的 `exact_exit_code_available`，completion 也只在确有 owned exit code 时标记为 true。
- 增加崩溃窗口、stale gate、gate 后退出、Goal 转移回滚、阶段写盘注错、终态观察顺序、detach drain、control pending、checkpoint 节流和状态字段测试。
- 全量测试最终为 121 项通过、3 项平台跳过；py_compile、Skill quick validation、diff check 与代码指标检查通过。
