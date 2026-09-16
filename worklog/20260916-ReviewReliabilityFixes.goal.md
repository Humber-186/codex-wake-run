# 审阅可靠性修复

## 目标

批判性核验并吸收针对 `0425159` 的代码审阅建议，修复 adopt 握手、阶段事件提交顺序、终态与阶段投递耦合、runtime checkpoint、状态展示和 control 超时语义中的可靠性问题。

## 状态

已完成。

## 交付范围

- adopt 尝试隔离、并发锁、Goal holder 可回滚转移与退出竞态处理。
- 统一 owned/adopted 阶段事件提交顺序并节流无事件 checkpoint。
- 阶段通知短时投递、终态观察先持久化、completion 中呈现阶段投递摘要。
- detach 明确排空已发生事件；control 超时返回 durable pending。
- 修正非健康 Run 的退出码精确性展示并补齐针对性测试与文档。

## 非目标

- 本次不实现运行中修改阶段计划、跨线程修改通知目标、历史 purge 或通用条件 DSL。
- 本次不增加中央 daemon、数据库或通用事务框架。

## 已交付

- adopt 使用唯一 attempt 握手文件和 Run 级进程锁；worker 在 `prepared` 前验证目标身份，Goal holder 转移可验证回滚，gate 后目标退出正常生成 `observed_exit`，握手清理失败降级为 warning。
- owned/adopted observer 共用阶段提交器，严格执行“本批 event 全部 durable → runtime checkpoint durable → 提交投递”；恢复 offset 同时参考已持久化 event，避免 checkpoint 失败后的重复错配。
- 阶段投递默认采用 20 秒短策略；退出观察先写 runtime，终态只等待当前活跃投递、其余保留 pending，adopted completion 先持久化再更新准确的 `stage_delivery` 汇总；detach 则完整排空并在 ack 中报告汇总。
- 普通 offset checkpoint 按 5 秒或 1 MiB 节流，阶段/终态边界立即落盘。
- control 超时返回 durable `pending`；detached/orphaned/lost 不再误报精确退出码可用。
- 明确一条日志行最多推进一个阶段，并同步 Skill、英文/中文 README 与回归测试。

## 验证

- `python3 -m py_compile scripts/*.py tests/*.py`
- `timeout 300s python3 -m unittest discover -s tests -q`：121 项通过，3 项 Windows 平台测试跳过。
- `quick_validate.py .`：Skill 校验通过。
- `git diff --check`：通过。
- 变更文件函数长度、位置参数数、文件大小与新增代码复杂度检查通过。
