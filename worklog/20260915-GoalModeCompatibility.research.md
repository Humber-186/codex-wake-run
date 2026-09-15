# Goal 模式兼容调研

本文件保存本任务采用的既有调研基线，原始来源为 `/tmp/goal-mode.md`。实施过程中若代码或真实测试证据与其冲突，以可复现证据为准。

## 结论

可以。对当前的 Codex CLI 0.154.0+，最可落地的方案就是：

> **在 wake-run 等待期间，把原本处于 `active` 的 Goal 暂时切换为 `paused`；后台任务完成后，先投递唤醒消息，再有条件地恢复为 `active`。**

但不能只实现成简单的：

```text
set paused
启动后台任务
任务完成
set active
```

它需要做成一个**可验证、可恢复、幂等的 Goal 暂停租约（Goal Guard / Goal Lease）**。

当前冲突的根源很明确：wake-run 的 Skill 要求脚本返回 `status: armed` 后立即结束当前 turn，后台 worker 完成后再通过 `codex queue` 唤醒；但 active Goal 在线程变 idle 后会自动启动下一轮 continuation，所以根本不会真正进入等待状态。

Codex 官方 App Server 已提供 `thread/goal/get`、`thread/goal/set` 和 `thread/goal/clear`，管理的就是 `/goal` 所使用的持久化状态，因此技术上可以自动 pause/resume。([OpenAI Developers][1]) 但 Codex 0.154.0 暴露给模型的 `update_goal` 工具只允许 `complete` 和 `blocked`，不能完整实现 pause/resume，所以不能只修改 Skill 提示词，必须由 wake-run 脚本通过 App Server API 完成。

---

## 推荐架构：Goal Guard

建议增加如下状态机：

```text
PREPARED
   ↓
GOAL_PAUSED_AND_VERIFIED
   ↓
TARGET_RUNNING
   ↓
COMPLETION_PERSISTED
   ↓
WAKE_QUEUED
   ↓
GOAL_RELEASED
   ↓
DONE
```

### 1. 启动阶段采用“两阶段 arm”

为了避免“Goal 已暂停，但 worker 还没成功启动”的危险窗口，不建议先 pause 再直接 `Popen()`。

更可靠的顺序是：

1. 启动 detached supervisor，但 supervisor 暂时不启动目标命令。
2. supervisor 写入 `prepared` 握手状态并等待启动门闩。
3. launcher 获取该 thread 的跨进程锁。
4. 调用 `thread/goal/get`。
5. 根据 Goal 状态建立租约：

   * `active`：需要暂停，并记录 wake-run 拥有这次暂停；
   * 已经 `paused`，且没有 wake-run 租约：视为用户原本就暂停，绝不能自动恢复；
   * `blocked`、`complete`、`usageLimited`、`budgetLimited`：保持原样；
   * 没有 Goal：不做任何 Goal 操作。
6. 在修改 Goal 前，原子写入租约文件。
7. 调用：

```json
{
  "method": "thread/goal/set",
  "params": {
    "threadId": "<CODEX_THREAD_ID>",
    "status": "paused"
  }
}
```

只修改 `status`，不重写 objective、budget 或 usage。

8. 再次调用 `thread/goal/get`，确认：

   * status 确实是 `paused`；
   * objective、createdAt、tokenBudget 等身份字段没有变化。
9. 写入 supervisor 的 commit gate。
10. supervisor 才真正启动目标命令，并完成现有 startup handshake。
11. launcher 返回：

```json
{
  "status": "armed",
  "goal_guard": {
    "mode": "paused",
    "verified": true,
    "lease_id": "..."
  }
}
```

如果 pause、验证、worker 启动中的任何一步失败，则：

* 不返回 `armed`；
* 不静默降级；
* supervisor 不启动目标，或立即终止目标；
* 已经由 wake-run 暂停的 Goal 必须回滚并读回验证。

这比“先暂停、后启动 worker”多一个很小的门闩状态，但显著降低 launcher 崩溃造成 Goal 永久 paused 的概率。

---

## 2. 完成阶段必须“先 queue，后 resume”

正确顺序是：

```text
目标命令结束
→ 原子持久化 completion event
→ codex queue 唤醒消息
→ 确认 queue 已接受
→ 释放 Goal lease
→ set active
→ get 并验证 active
```

**不能先恢复 Goal，再发送 queue。**

原因是 Codex 在外部将 Goal 设置为 `active` 时，会直接尝试 `continue_if_idle()`；如果此时线程空闲，Goal continuation 可能立即启动。 若随后 `codex queue` 失败，线程会恢复 Goal 自动执行，却收不到后台任务的完成信息。

先 queue 的失败模式更安全：

* queue 失败：Goal 仍 paused，不会空转，worker 可以重试；
* queue 成功、resume 失败：完成消息仍会唤醒线程，Goal 暂时保持 paused，恢复操作可以独立重试；
* 两者成功：完成消息优先进入线程，随后 Goal 恢复自动推进。

另外，不能通过：

```bash
codex queue --message "/goal resume"
```

来代替 API 调用。`codex queue` 提交的是普通 `UserInput::Text`，不是在 TUI 中执行 Slash Command。

---

## 3. Goal 恢复必须是“有条件恢复”

worker 不能无条件执行：

```text
status = active
```

租约中至少应保存：

```json
{
  "schema_version": 1,
  "lease_id": "...",
  "thread_id": "...",
  "original": {
    "status": "active",
    "objective": "...",
    "created_at": 123,
    "token_budget": null,
    "tokens_used": 1000,
    "time_used_seconds": 20
  },
  "paused_snapshot": {
    "updated_at": 456
  },
  "holders": ["run-id"],
  "phase": "paused"
}
```

恢复前重新 `thread/goal/get`，只有满足以下条件才允许恢复：

* 该租约原始状态确实是 `active`；
* 当前状态仍是 `paused`；
* objective 与 createdAt 未改变；
* tokenBudget 未改变；
* paused 后的 updatedAt、tokensUsed、timeUsedSeconds 没有异常变化；
* Goal 没有被 clear、replace、blocked 或 complete。

出现任何冲突都应：

```text
restore_status = skipped_conflict
```

而不是“帮用户修回来”。

这可以正确处理：

* 用户等待期间手动恢复 Goal；
* 用户修改 Goal objective；
* 用户清除 Goal；
* 用户再次手动 pause，表示希望继续保持暂停；
* 其他工具或 Codex 自身改变 Goal 状态。

### 一个无法完全消除的窄竞态

Codex 内部 Goal 状态带有 `goal_id`，内部更新也会利用它避免写错目标；但 0.154.0 的公开 App Server Goal 对象和 `thread/goal/set` 参数没有向外部客户端提供完整的 `expectedGoalId` 或 revision 条件写接口。

所以在：

```text
最终 get 验证
→ set active
```

这两个请求之间，如果用户恰好替换了 Goal，客户端无法做到严格的原子 CAS。

对少数本地 Linux 用户，通过以下措施已经足够稳健：

* 每 thread 文件锁；
* 恢复前完整 fingerprint 检查；
* 恢复后读回验证；
* 冲突时 fail-safe，不恢复；
* 将时间窗口控制在一次连续 RPC 会话内。

但若要求形式上的并发原子保证，需要 Codex 上游增加：

```text
expectedGoalId
expectedUpdatedAt / revision
```

或提供一个原子的：

```text
thread/queue/addAndResumeGoal
```

接口。

---

## 4. 多个 watcher 要共享同一个 thread lease

当前项目允许同一 thread 启动多个 watcher，因此不能让每个 watcher 各自执行 pause/resume。

建议使用：

```text
~/.codex/wake-run/goal-leases/<thread-id>.json
```

作为全局 per-thread lease，而不是把租约放在某个项目的日志目录中。

按 wake-run 当前“任一事件完成即可唤醒”的语义：

* 第一个 watcher 创建 lease 并暂停 Goal；
* 后续 watcher 发现 wake-run 已拥有该 paused 状态，只加入同一个 lease；
* **任意一个 watcher 首次成功 queue 完成消息后，即可释放整个 lease 并恢复 Goal；**
* 其他仍运行的 watcher之后只发送各自的完成消息，不再 pause/resume Goal。

这避免了以下错误：

```text
A 创建 pause
B 看到已经 paused，以为是用户暂停
B 先完成，但不恢复
Goal 一直等到 A 完成才恢复
```

如果实际需求是“所有进程完成后才唤醒”，应让用户监视一个 wait-all 包装进程，而不是改变单个 wake-run watcher 的默认语义。

---

## 5. 投递状态必须拆分

当前 completion delivery 不应再只有一个笼统的 `delivered`。建议拆成：

```json
{
  "wake_delivery": {
    "state": "pending | queued",
    "attempts": 1,
    "wake_id": "..."
  },
  "goal_release": {
    "state": "not_needed | pending | restored | skipped_conflict | retrying"
  }
}
```

这样能够正确恢复以下崩溃点：

| 崩溃位置                              | replay 行为                 |
| --------------------------------- | ------------------------- |
| completion 已保存，queue 未成功          | 只重试 queue                 |
| queue 已成功，Goal 未恢复                | 不重新运行目标，只重试 Goal release  |
| Goal 已恢复，状态文件未更新                  | `get` 发现已 active，按幂等成功处理  |
| 用户改变了 Goal                        | 标记 `skipped_conflict`，不覆盖 |
| supervisor 在 pause 前退出            | 没有 Goal 副作用               |
| supervisor 在 pause 后、target 启动前退出 | 根据 lease 回滚 Goal          |

现有项目已经遵循“先持久化 completion，再通过 `codex queue` 投递”的方向；需要做的是把 Goal release 纳入同一个持久化状态机，而不是塞进一次不可恢复的 `finally`。

---

## 6. 失败策略应当 fail closed

建议增加：

```text
--goal-policy auto      # 默认
--goal-policy require
--goal-policy ignore
```

其中：

* `auto`

  * active Goal 自动保护；
  * 没有 Goal 或已非 active 则保持原样。
* `require`

  * 检测到 active Goal 时，必须成功 pause 并验证；
  * App Server 不可用或验证失败则不运行目标。
* `ignore`

  * 明确保留旧行为；
  * 输出警告，供了解风险的用户使用。

默认绝不能在 Goal API 失败后继续返回普通 `status: armed`，否则表面看起来已等待，实际 Goal 仍在高速 continuation。

---

## 7. App Server 客户端实现建议

对这个小型独立项目，我更倾向于新增一个很薄的：

```text
scripts/wake_run_goal.py
```

通过与当前会话相同的 `codex` 可执行文件和同一 `CODEX_HOME`，使用 App Server JSON-RPC：

* initialize；
* initialized；
* `thread/goal/get`；
* `thread/goal/set`；
* 读回验证；
* 超时退出。

不建议：

* 直接修改 Codex SQLite 数据库；
* 依赖 TUI 键盘自动化；
* 将 `/goal pause` 当成 queue 消息；
* clear 后重建 Goal；
* 用 `blocked/stalled` 冒充正常等待。

直接改 SQLite 虽然可以碰到内部状态，但数据库文件名、schema、迁移版本和 runtime notification 都不是 wake-run 应该承担的兼容面。

此外，应优先连接当前会话实际使用的共享 App Server/daemon endpoint。若只能另起一个临时 App Server 进程，它可以修改持久化 Goal 状态，但未必持有当前 TUI 的 live `GoalRuntime`。从实现看，只有找到 live runtime 时，外部 Goal 修改才会先结算当前 turn 并应用 runtime effects；而 turn-stop 的普通 accounting 又只结算 active/budget-limited 状态。由此推断，在 embedded TUI + 外部临时 App Server 的组合下，当前这个 pause turn 的 Goal usage 可能少计一次。

这不影响“阻止自动 continuation”的核心效果，但若用户依赖严格的 Goal token budget，必须把这一点纳入真实环境测试，或者要求使用可复用的 daemon endpoint。

---

## 8. 更理想的长期方案：Goal continuation deferral

Codex 内部实际上已经有一个比 pause/resume 更贴合该场景的机制：

* `continue_if_idle()` 在启动 Goal continuation 前检查 continuation deferral；
* 有 deferral 时保持 Goal 为 active，但不自动启动下一 turn；
* 下一次真正的 turn 开始时，deferral 自动清除。

这正好对应 wake-run：

```text
Goal 保持 active
→ 设置 defer
→ 当前 turn 正常结算
→ 等待后台任务
→ codex queue 完成消息
→ queued turn 开始，自动清除 defer
→ queued turn 结束后 Goal 正常继续
```

它优于 pause/resume，因为：

* 不改变用户可见 Goal 状态；
* 不需要恢复 active；
* 不会混淆“用户暂停”和“wake-run 暂停”；
* 更容易保留 Goal 的 token/time accounting；
* 不存在 resume-before-queue 竞态。

但目前它是 Codex 内部实现，没有公开的 App Server API。因此：

* **不要直接向内部 deferral SQLite 表写数据；**
* 可以向 Codex 上游提出一个很小的接口：

```json
{
  "method": "thread/goal/defer",
  "params": {
    "threadId": "...",
    "leaseId": "...",
    "releaseOn": "nextExternalTurn"
  }
}
```

从架构上看，这才是最终最干净的方案。对未修改的 stock Codex 0.154.0+，仍应先采用公开 Goal API 的 pause lease。

---

## 9. 必须通过的集成测试

除了单元测试，发布前至少需要在真实 Linux Codex CLI 0.154.0 上验证：

1. active Goal + `sleep 10`：

   * `armed` 后没有任何 Goal continuation；
   * 任务结束后 completion message 先出现；
   * Goal 最终恢复 active。
2. 用户原本 paused：

   * 完成后仍 paused。
3. 等待期间用户 clear/change/block/resume Goal：

   * wake-run 不覆盖用户操作。
4. queue 暂时失败：

   * Goal 保持 paused；
   * queue 重试成功后才恢复。
5. restore 暂时失败：

   * completion message仍能送达；
     -只重试 restore，不重复运行目标。
6. 两个 watcher，第二个先完成：

   * 第二个可以唤醒并释放共享 lease。
7. launcher 在不同阶段被 `SIGKILL`：

   * lease 可被 replay/recovery 正确处理。
8. 进程完成后 worker 被 `SIGKILL`：

   * completion event 和 Goal lease 可以幂等恢复。
9. 连续多次运行：

   * 不出现 stale lease、重复 resume 或丢失 completion。

这里尤其不能只依赖源码推断。2026 年 8 月仍有一个开放的 Desktop 问题报告称，持久化状态已经是 `paused` 时仍收到了 Goal continuation；而 0.154.0 CLI 源码的正常路径确实会在 continuation 前重新读取持久化 Goal，并在非 active 时停止。  因此，**真实目标版本的端到端测试应当是 release gate，而不是可选项**。

另一个 0.154.0 报告也印证了原问题的严重性：active Goal 在 idle 后会很快重新启动，等待外部进程可能演变成不断读取完整上下文的轮询 turn。

---

## 建议的项目改动

最终可以控制在以下范围内，不需要把项目重构成大型服务：

* 新增 `wake_run_goal.py`：Goal RPC、snapshot、pause、restore、verification。
* `wake_run_state.py`：增加全局 per-thread lease、文件锁和恢复状态。
* `wake_run_core.arm_watcher()`：改成 supervisor prepared → Goal commit → target start 的两阶段启动。
* `wake_run_worker.py`：完成后执行 persist → queue → release lease。
* `deliver_completion_event()`：拆分 wake delivery 与 Goal release 状态。
* `--replay-pending`：同时处理遗留 completion 和遗留 Goal lease。
* `SKILL.md`：只有 `goal_guard.verified=true` 时才允许将 active Goal 场景视为安全 armed。
* 增加真实 0.154.0 CLI 集成测试。

因此，我的最终判断是：

> **你的 pause/resume 思路是当前 stock Codex 上最现实的方案，但必须实现成“持久化 Goal 租约 + 读回验证 + queue-first + 冲突保护 + 崩溃恢复”。简单 toggle 不够可靠。**
>
> **长期最理想的是推动 Codex 暴露现有 continuation-deferral 机制，那样 wake-run 无需改变 Goal 状态。**

[1]: https://developers.openai.com/?utm_source=chatgpt.com "Codex App Server"

