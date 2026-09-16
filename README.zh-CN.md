<h1 align="center">wake-run-skill</h1>

<p align="center">一个 Codex Skill：用分离的守护进程运行长命令，在声明的日志里程碑和进程终态唤醒原 Codex 线程，模型无需轮询。</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"> <a href="https://linux.do/latest"><img src="https://img.shields.io/badge/Linux.do-Community-7C3AED?style=flat-square" alt="Linux.do 社区"></a>
</p>

在 agent 会话里跑长实验，通常只有两种难受的选择：要么让模型停在轮询循环里，一轮一轮地等；要么放弃这条线程，等结果出来之后重新把任务背景讲一遍。

wake-run 把这段等待去掉。你把已经定稿的命令交给它，它启动一个分离的守护进程，以可验证的暂停租约保护当前 active Goal，并完成两阶段启动握手；只有 Goal 保护已提交、监控用 shell 进程成功启动且后续退出状态已由 watcher 接管后，才打印 `status: armed`，随后当前轮次结束。这不代表底层应用已经完成初始化、取得许可证或通过配置检查。守护进程阻塞在操作系统的进程退出事件上。命令结束时，无论成功还是失败，守护进程都会先持久化完成事件，再用 `codex queue` 把唤醒消息注入原线程，最后有条件地恢复 Goal。也可以显式启用经济看护，让 Luna 等低成本模型只在执行事件发生后分诊结果，并在有限授权下重试完全相同的命令。

## 核心特性

| 特性 | 为什么重要 |
|---|---|
| 长任务事件驱动 | 独立等待线程阻塞在 `process.wait()`；轻量 supervisor 只处理日志增量和显式控制命令。 |
| 有序阶段唤醒 | 一个 EDA 进程可按 DC、布局、CTS、布线等一次性日志正则依次唤醒，最后仍有唯一终态事件。 |
| 明确生命周期 | `stop` 终止进程树，`detach` 保留目标并解除监督，`adopt` 恢复观察失去 worker 的 Linux Run。 |
| 严格启动确认 | worker 报告监控用 shell 进程 PID 后才返回 `armed`；启动失败和超时都会明确报错。 |
| active Goal 保护 | 目标启动前用持久化共享租约暂停并验证 Goal；完成消息成功入队后才有条件恢复。 |
| 唤醒同一条线程 | 守护进程调用 `codex queue --thread "$CODEX_THREAD_ID"`，续跑消息回到发起任务的那次对话。 |
| 失败始终可见 | 命令非零退出会唤醒线程；worker 或目标进程启动失败会在返回 `armed` 前同步报错。 |
| 明确稳定基线 | 稳定目标是少量可信用户、Linux 与 Codex CLI 0.154.0+；Windows 路径仍保留，但不属于当前可靠性承诺。 |
| 持久化投递状态 | 每次运行具有独立日志和原子 completion JSON，记录投递次数、错误与最终状态。 |
| 后台任务可发现 | 每次运行持久化 spec/runtime 并写入轻量全局索引，可按当前线程列出或按 `run_id` 查询。 |
| 可选经济看护 | 独立的低成本 Codex 会话按事件分诊；模型、证据范围和原命令重试次数均由主 agent 的计划明确授权。 |
| 结构化递归防护 | 监护角色不能再次启动或补发 wake-run；重试由现有 worker 内部执行，不会创建嵌套 watcher。 |

## 架构

```text
Codex thread
    │
    │ 启动 wake-run
    ▼
wake_run.py launcher
    │
    │ detached worker + 目标 PID 握手
    ▼
实验进程
    │
    │ process.wait() + 有序日志里程碑
    ▼
退出码 + 日志
    │
    ├─ 直接模式：持久化完成事件
    │
    └─ 经济看护：按策略创建或恢复只读监护会话
            ├─ 已授权 retry_exact ──► 原命令再次执行
            └─ 成功 / 升级 / 监护异常
    │
    │ codex queue
    ▼
Codex 被唤醒并继续原任务
```

启动器在目标启动前验证 `codex queue` 与 App Server Goal 接口。这样，不兼容的 Codex CLI 会立刻报错，不会让任务处于虚假的 `armed` 状态。

## 使用示例

**你：**

```text
帮我用 wake-run 运行这个实验，结束后继续完成任务。
```

**Codex** 启动任务并拿到 `armed`：

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "process_pid": 97154, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log", "goal_guard": {"mode": "paused", "verified": true, "lease_id": "...", "runtime_scope": "detached", "current_turn_accounting": "not_guaranteed"}}
```

随后当前轮次结束，不会轮询后台任务。

脚本退出时，守护进程会向同一条线程写入：

```text
[后台任务完成-系统提示]
任务：echo "training started"; sleep 2; echo "done"
日志：/work/project/.codex-wake-run/b7599ab35869.log
exit_code: 0
wall：2.003s
user：0.012s
sys：0.004s
run_id：b7599ab35869
wake_id：5e9ca210a8c84d9d97b66a9ec0a79d58
```

Codex 在收到 `armed` 后只发送一条简短确认并结束当前轮次；收到唤醒后再根据需要读取日志并继续原任务。

不会自主轮询不等于无法查询。用户明确询问时，可以做一次确定性查询：

```bash
python <skill-dir>/scripts/wake_run.py --list --active
python <skill-dir>/scripts/wake_run.py --show <run_id>
```

启动时可用 `--name route-opt` 添加便于辨认的名称。`--list` 默认只列出当前 Codex thread 发起的 Run；全局索引位于 `${CODEX_HOME:-~/.codex}/wake-run/index/`，实际 spec/runtime 仍保存在对应日志目录。

### 多阶段 EDA 运行

阶段计划是静态、有序、一次性的日志行正则：

```json
{
  "schema_version": 1,
  "stages": [
    {"id": "dc", "pattern": "Design Compiler completed"},
    {"id": "place", "pattern": "place_opt completed"},
    {"id": "cts", "pattern": "clock_opt completed"},
    {"id": "route", "pattern": "route_opt completed"}
  ]
}
```

```bash
python3 <skill-dir>/scripts/wake_run.py \
  --command './run_flow.sh' \
  --name full-chip-flow \
  --stage-plan /absolute/path/stages.json
```

只有完整日志行会参与匹配，只检查下一个未完成阶段，且一行最多推进一个阶段。一次扫描发现的所有事件会先全部持久化，再推进 runtime checkpoint，最后提交投递；事件和终态 checkpoint 立即落盘，普通 offset-only checkpoint 则按 5 秒或 1 MiB 节流。阶段投递默认只短暂重试 20 秒（`WAKE_RUN_STAGE_QUEUE_TIMEOUT`）；进程退出时至多等待当前正在进行的投递，尚未开始的事件保留为 pending 交给 replay，失败与 pending 数量写入最终 completion。阶段事件使用稳定的 `wake_id`，不释放 Goal；退出观察在这段有界等待前持久化。第一版刻意不加入任意脚本条件、持续模型判断、elapsed 或 idle 猜测。

### 运行控制与恢复

```bash
python <skill-dir>/scripts/wake_run.py --stop <run_id>
python <skill-dir>/scripts/wake_run.py --detach <run_id>
python <skill-dir>/scripts/wake_run.py --adopt <run_id>
```

`stop` 对目标进程树执行 TERM/KILL 收口，并产生 `cancelled` 终态唤醒。`detach` 会先排空已持久化阶段事件的短时投递尝试并报告汇总，再释放该 Run 的 Goal holder、结束 worker；目标进程继续运行，不再保证退出码或最终通知。控制命令已经持久化但 15 秒内尚未收到 worker 确认时，会返回 `status: pending`，而不是误报失败。`adopt` 只用于原 worker 已死而目标仍活着的恢复路径：每次尝试使用唯一握手 ID 和 Run 级锁，转交失败会回滚 Goal holder；它用 Linux boot ID 与 `/proc/<pid>/stat` starttime 排除 PID 复用，继续未完成的阶段观察，gate 后目标退出则正常落为 `observed_exit`。由于新 observer 不是目标父进程，最终消息会明确给出 `observer_mode: adopted` 和 `exact_exit_code_available: false`。健康 Run 不允许 adopt。

`wall` 是墙钟耗时；`user` 和 `sys` 分别是用户态与内核态 CPU 耗时。Windows 下无法可靠统计整棵进程树的 CPU 时间，因此省略 `user` 和 `sys`，不会使用 PowerShell 宿主进程的不完整数据。

唤醒采用 at-least-once（至少一次）投递语义。重试始终复用同一个 `wake_id`，因此重复消息代表同一个完成事件，不应重复执行已经完成的后续动作。每次投递前，`<run_id>.completion.json` 会记录 `pending`、`delivering` 或 `delivered` 状态以及尝试详情。未送达事件可以显式补发：

```bash
python <skill-dir>/scripts/wake_run.py --replay-pending --log-dir <运行状态目录>
```

Goal 保护默认使用 `--goal-policy auto`。`require` 会在当前线程没有 active Goal 时拒绝启动；`ignore` 会显式禁用保护，只应在可以接受等待期间 Goal 自动续跑时使用。completion JSON 分别跟踪唤醒投递和 Goal 释放，因此只重试恢复 Goal 时不会重新运行目标，也不会重复已经成功入队的唤醒消息。

Goal Guard 通过独立的 stdio App Server 修改持久化 Goal，能够可靠阻止 idle continuation，但它不在当前 Codex TUI 的 live runtime 内。因此，启动 wake-run 的当前 turn 可能不会计入 Goal 的 `tokensUsed`、`timeUsedSeconds` 或预算判定；`runtime_scope: detached` 与 `current_turn_accounting: not_guaranteed` 会明确报告这一限制。Codex CLI 0.154.0 没有向 Skill 暴露当前 TUI 的 live Goal runtime endpoint，wake-run 不会伪装成完整记账。

## 经济看护

经济看护不是模型轮询器。操作系统 watcher 仍然通过 `process.wait()` 等待，监护模型只在一次执行结束后调用。主 agent 先创建严格的 JSON 计划：

```json
{
  "schema_version": 2,
  "model": "gpt-5.6-luna",
  "review_on": ["failure"],
  "instructions": "成功时总结结果；只对明确的瞬时外部服务故障重试，其他失败升级主 agent。",
  "allowed_actions": ["retry_exact"],
  "max_exact_retries": 1,
  "log_tail_bytes": 65536
}
```

然后显式启用：

```bash
python3 <skill-dir>/scripts/wake_run.py \
  --command '<完全确定的命令>' \
  --monitor-plan '<计划 JSON 的绝对路径>'
```

launcher 会在目标进程启动前验证 `codex exec`/`resume` 能力并持久化监护策略，但不会为 readiness 单独调用一次模型。`review_on: ["failure"]` 的成功任务是 0 次 Luna 调用；首次需要审查时才创建只读会话并直接完成分诊，后续重试再 resume。每次调用都显式施加只读 sandbox、固定状态目录 cwd 与非 Git 目录许可；失败时明确写入 completion 并唤醒，不会退回直接模式或替换模型。唤醒消息会携带真实的监护动作、分类、摘要和原因。

只有完整命令在先前执行可能已产生部分副作用后仍可安全重复时，才应授权 `retry_exact`。外部瞬时故障本身不能证明命令没有产生副作用；部署、发布、付款和数据库迁移等命令通常不应允许自动重试。

经济看护只审查执行终态；阶段事件是确定性日志匹配，不调用 Luna，也不宣称检测运行中的无响应任务。完整计划约定见 [`references/economic-monitor.md`](./references/economic-monitor.md)。

## 快速开始

这个仓库本身就是一个独立 Skill，不需要 `.codex-plugin`，也没有额外的 `skills/wake-run` 套娃目录。

让 Codex 安装：

```text
帮我安装 wake-run 这个 skill：https://github.com/Humber-186/codex-wake-run
```

安装后直接使用：

```text
帮我调用 wake-run 这个 skill 执行 xxx 任务。
```

几件值得知道的事：

- **只能在 Codex 会话内工作。** Skill 依赖 `CODEX_THREAD_ID` 判断应该唤醒哪条线程。
- **稳定支持基线是 Linux + Codex CLI 0.154.0+。** 启动器会预检 `codex queue`，并在目标启动前读回验证 App Server Goal 接口。`--goal-policy ignore` 是显式退出保护。
- **Goal RPC 有明确时限且失败可见。** `WAKE_RUN_GOAL_TIMEOUT` 默认 10 秒，必须为有限正数；超时或协议错误会让启动失败，不会静默降级。
- **经济看护还需要持久 `codex exec` 会话、结构化输出和 `codex exec resume`。** 监护调用超时由显式的 `WAKE_RUN_MONITOR_TIMEOUT` 控制，默认 300 秒。
- **它不是绕过沙箱的手段。** 后台进程继承启动环境及其权限。
- **每个实验一个 watcher。** 任务确实需要时可以并行运行多个；Goal lease 使用阻塞式操作系统锁串行化短暂变更。任一 dead worker 在 `spawning/running` 阶段缺少 completion 时，整个 lease 固化为 `orphaned`，拒绝新 watcher 加入和普通恢复。
- **公开 Goal API 没有 CAS。** wake-run 会按公开身份字段严格校验快照，但无法消除读取与更新之间极窄的外部并发修改窗口。

## 仓库结构

| 路径 | 内容 |
|---|---|
| [`SKILL.md`](./SKILL.md) | Skill 指令：启动流程、运行时约定、唤醒消息结构。 |
| [`scripts/wake_run.py`](./scripts/wake_run.py) | 命令行入口。 |
| [`scripts/wake_run_core.py`](./scripts/wake_run_core.py) | 唤醒投递、Goal 释放顺序与补发。 |
| [`scripts/wake_run_app_server.py`](./scripts/wake_run_app_server.py) | 有界的 Codex App Server JSONL 客户端。 |
| [`scripts/wake_run_goal.py`](./scripts/wake_run_goal.py) | Goal 共享租约、验证、冲突处理与恢复。 |
| [`scripts/wake_run_goal_state.py`](./scripts/wake_run_goal_state.py) | Goal lease schema 与 holder 阶段。 |
| [`scripts/wake_run_stages.py`](./scripts/wake_run_stages.py) | 阶段计划校验与增量日志匹配。 |
| [`scripts/wake_run_stage_commit.py`](./scripts/wake_run_stage_commit.py) | event 先于 checkpoint 的持久提交与 checkpoint 节流。 |
| [`scripts/wake_run_supervisor.py`](./scripts/wake_run_supervisor.py) | 运行期阶段扫描、投递队列与控制命令。 |
| [`scripts/wake_run_control.py`](./scripts/wake_run_control.py) | `stop` / `detach` 命令与确认通道。 |
| [`scripts/wake_run_adopt.py`](./scripts/wake_run_adopt.py) | Linux 恢复 observer 的验证与启动。 |
| [`scripts/wake_run_launcher.py`](./scripts/wake_run_launcher.py) | 两阶段 supervisor 与目标启动。 |
| [`scripts/wake_run_monitor.py`](./scripts/wake_run_monitor.py) | 监护计划、Codex 会话、结构化分诊与递归防护。 |
| [`scripts/wake_run_registry.py`](./scripts/wake_run_registry.py) | Run spec/runtime、全局索引以及 list/show 查询。 |
| [`scripts/wake_run_models.py`](./scripts/wake_run_models.py) | worker 与 monitor 共用的不可变运行记录。 |
| [`scripts/wake_run_worker.py`](./scripts/wake_run_worker.py) | 目标进程执行与完成事件持久化。 |
| [`scripts/wake_run_metrics.py`](./scripts/wake_run_metrics.py) | 墙钟与 CPU 耗时统计。 |
| [`scripts/wake_run_process.py`](./scripts/wake_run_process.py) | 跨平台目标进程组与失败清理。 |
| [`scripts/wake_run_state.py`](./scripts/wake_run_state.py) | 原子状态持久化、文件权限与投递锁。 |
| [`scripts/wake_run_platform.py`](./scripts/wake_run_platform.py) | Windows 与 POSIX 命令构造。 |
| [`agents/openai.yaml`](./agents/openai.yaml) | 面向 agent 的 Skill 元数据，已开启隐式调用。 |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | 单元测试、集成测试、Windows 回归测试。 |
| [`tests/test_recovery.py`](./tests/test_recovery.py) | 恢复、持久化失败与补发测试。 |
| [`tests/test_monitor.py`](./tests/test_monitor.py) | 经济看护计划、协议、授权重试和端到端测试。 |
| [`tests/test_process.py`](./tests/test_process.py) | POSIX 与 Windows 进程树清理测试。 |
| [`tests/test_goal_worker.py`](./tests/test_goal_worker.py) | Goal 启动窗口、orphan 与阻塞锁测试。 |
| [`tests/test_lifecycle.py`](./tests/test_lifecycle.py) | 多阶段、stop、detach、adopt 与补发集成测试。 |
| [`tests/test_reliability_review.py`](./tests/test_reliability_review.py) | 崩溃窗口、投递顺序、控制超时与状态报告回归测试。 |
| [`references/economic-monitor.md`](./references/economic-monitor.md) | 经济看护计划与决策契约。 |

运行日志默认写入启动任务所在项目的 `.codex-wake-run/` 目录。仓库自身会忽略该目录，但宿主项目不会自动继承本仓库的 `.gitignore`；请在宿主项目中自行加入 `.codex-wake-run/`。大型 EDA 项目可通过 `--log-dir ~/.codex/wake-run/<project>` 把高频状态写入本地磁盘，避免源码仓库或 NFS 路径。

## 致谢

感谢 [Linux Do](https://linux.do/latest) 社区的支持。
