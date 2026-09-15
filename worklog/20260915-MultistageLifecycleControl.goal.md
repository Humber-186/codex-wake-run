# 多阶段唤醒与运行生命周期控制

## 目标

- 同一个长任务可按有序日志里程碑多次唤醒原 Codex 线程，并在进程退出时保留一次终态唤醒。
- 为活动 Run 提供语义明确的 `stop`、`detach` 和故障恢复用途的 `adopt`。
- 保持单 Run 单 worker、JSON 状态和无常驻中心服务的轻量架构。
- 中间阶段不释放 Goal；停止或自然终止时收口，detach 显式解除监督并释放 Goal。

## 状态

已完成。

## 交付成果

- 新增静态、有序、一次性的日志阶段计划；阶段事件持久化、可补发，且不会释放 Goal。
- worker 改为等待线程与轻量 supervisor 协作，可在目标运行期间扫描阶段并处理控制命令。
- 新增 `--stop`、`--detach`、`--adopt` CLI；stop 生成 cancelled 终态，detach 保留目标并释放监督，adopt 恢复孤立 Run 的观察。
- adopt 使用 Linux boot ID 与进程 starttime 校验目标身份，并在终态明确报告无法取得精确退出码。
- list/show runtime 展示阶段进度、observer 模式、最近事件和精确退出码能力。
- README、中文 README、Skill 指令与 UI 描述已同步。
- 自动化覆盖多阶段顺序/完整行、阶段补发、stop、detach、adopt 与 Goal holder 恢复；全套 110 项测试通过，3 项 Windows 专用测试按平台跳过。
