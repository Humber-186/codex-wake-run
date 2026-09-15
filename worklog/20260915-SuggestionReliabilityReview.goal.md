# 建议审查与可靠性改进

## 预期目标

- 逐项验证 `/tmp/suggestion.txt`，只采纳能改善正确性、可靠性或 Skill 表述准确性的建议。
- 修复 Windows 锁存活探测、监护会话恢复约束、工作目录校验与失败清理等已确认问题。
- 为采纳的行为补充自动化测试，并保持现有直接模式和经济看护模式契约清晰。

## 行进状态

- 已完成：高优先级可靠性缺陷、恢复隔离、编码与 Skill 契约均已修正并验证。

## 最终已交付成果

- Windows delivery lock 改用 WinAPI 无副作用 PID 存活探测。
- 经济看护每次 resume 都重新施加只读 sandbox、固定状态目录 cwd 与非 Git 目录许可；真实 Luna smoke 已通过。
- 启动前严格拒绝不存在的 cwd，握手、状态写入或 wait 失败会清理目标进程树。
- 损坏 completion 不再阻塞其他正常 pending 事件补发，且错误保持显式可见。
- Codex 文本 I/O 固定为 UTF-8，递归防护覆盖隐藏 worker 模式。
- README、Skill 和经济看护 reference 已修正触发范围、`armed` 含义、安装地址、重试安全边界及 `.gitignore` 说明。
- GitHub Actions workflow 增加手动触发和 10 分钟 job 超时；远端仍无历史运行，文档不宣称未经验证的 Windows 稳定性。
- 最终本地验证：共 63 项测试，60 项通过、3 项 Windows 专属跳过；Python 编译、Skill validator、diff 检查和代码度量全部通过。
