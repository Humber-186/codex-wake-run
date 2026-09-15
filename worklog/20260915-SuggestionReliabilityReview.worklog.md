# 建议审查与可靠性改进工作日志

## 2026-09-15

- 阅读并核对 `skill-creator` 指令与 `/tmp/suggestion.txt` 全文。
- 确认两个高优先级问题：Windows 不应使用 `os.kill(pid, 0)` 探测存活；monitor resume 必须重新显式设置只读 sandbox、状态目录 cwd 与跳过 Git 检查。
- 确认 cwd 当前可能被默认日志目录创建间接“纠正”为错误的新目录，需要在任何写入前严格校验。
- 初步采纳损坏 completion 隔离、Codex 文本 I/O UTF-8、递归防护覆盖隐藏 worker、Skill/README 语义修正。
- 暂不采纳统一固定 5 秒重试：该建议引入无场景区分的时序策略，缺少足够依据且不属于正确性修复。
- 使用本机 Codex CLI 0.154.0 核对 `exec`/`exec resume` 参数：resume 子命令不直接暴露 `--sandbox` 与 `--cd`，但父级参数放在 `resume` 前可正确解析；据此修正调用顺序。
- 实现 Windows WinAPI 无副作用 PID 存活探测，移除 Windows 锁路径上的 `os.kill(pid, 0)`。
- monitor resume 现在每次重新施加 `read-only`、状态目录 cwd 与 `--skip-git-repo-check`，Codex 文本 I/O 显式采用 UTF-8。
- 启动前严格验证 cwd，确保拼错路径不会被默认状态目录间接创建。
- 新增独立目标进程组与跨平台进程树清理；握手失败、startup 状态写入失败及 `process.wait()` 异常都会清理目标树。
- replay 改为逐个隔离 completion 解析错误：损坏文件进入 `failures`，其他有效 pending 事件继续投递。
- 递归防护覆盖隐藏 `--worker` 模式，并收紧 Skill 触发范围、`armed` 语义、重试安全边界和 `.gitignore` 说明。
- 首轮扩展测试共 60 项，通过 58 项、Windows 专属跳过 2 项；随后调整测试职责拆分以满足单文件 500 行约束，完整测试通过。
- 不采纳顶层统一 JSON 异常包装：非零退出与 stderr/traceback 已能明确暴露故障，统一捕获反而会削弱 debug-first 信息。
- 不拆分 `SKILL.md` 的运行时契约：当前入口仅 78 行，且经济看护细节已经独立到 reference，继续拆分没有足够收益。
- 真实 smoke 在非 Git 临时目录创建并恢复 `gpt-5.6-luna` 监护会话，得到结构化 `report_success`；测试会话 `01a0a2bf-d4bf-7410-900d-e123f7ee8e12` 随后已归档。
- GitHub API 只读核对确认远端当前仍为 0 次 Actions 运行；仓库 workflow 已存在于 `main`，本次补充手动触发入口与 10 分钟 job 超时，但 Actions 仓库设置需要外部授权，不能靠源代码伪造“已跑绿”。README 因此明确以实际 Actions 结果为发布可靠性依据。
- 新增真实 POSIX 父子进程树终止测试，确认对独立进程组发送终止信号会覆盖后代进程。
- 最终执行 `PYTHONWARNINGS=error timeout 90s python3 -m unittest discover -s tests -v`：共 63 项，60 项通过、3 项 Windows 专属跳过。
- `python3 -m compileall -q scripts tests`、Skill `quick_validate.py`、`git diff --check`、500 行文件上限与 100 行函数上限检查全部通过。
- 增加 Windows `codex.cmd` 中文多行消息真实 shim 测试；该用例与既有 PowerShell 用例将在 Windows Actions 环境执行。
