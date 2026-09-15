# Run 可发现性与经济监护改进工作日志

## 2026-09-15

- 阅读项目说明、Skill 契约、现有实现和 `/tmp/suggestion2.txt`；指定的 `.md` 不存在，实际建议文件为 `/tmp/suggestion2.txt`。
- 确认基线测试共 93 项通过，3 项 Windows 专属测试跳过。
- 采纳持久 Run 记录、全局索引、查询入口、监护审查范围、懒创建和摘要送达。
- 暂不采纳运行期 policy 引擎、每秒 supervisor loop、stop/detach/adopt、多阶段条件和通用事件系统；当前缺少真实需求，且会扩大进程控制与恢复状态机。
- 不迁移现有平铺状态目录；用独立 spec/runtime 文件补足可发现性，避免破坏 completion/replay 布局。
- 新增 `wake_run_registry.py` 和共享不可变模型模块；launcher 在启动前建立 Run 记录，worker 持久化 prepared/running/completed 状态。
- CLI 新增 `--name`、`--list --active` 与 `--show`；损坏索引逐项报告，不隐藏其他有效 Run。
- monitor plan 升级为 schema v2，新增 `review_on`；schema v1 兼容映射为成功/失败都审查。
- 将不可变 monitor policy 与 session/call count runtime 分离；launcher 只预检 `codex exec`/`resume`，首次实际审查直接创建 session 并分诊。
- completion 与唤醒消息新增监护状态及真实 action/category/summary/reason；修复 failure-only 精确重试成功后误展示先前失败摘要的语义边角。
- 依据 `skill-creator` 保持 `SKILL.md` 为精简路由与关键约束，将完整 schema 和模式细节保留在 `references/economic-monitor.md`。
- 新增 Run registry、CLI 查询、failure-only 零模型调用、懒 session、摘要送达及跳过成功语义测试。
- 最终执行 `PYTHONWARNINGS=error timeout 90s python3 -m unittest discover -s tests -v`：101 项通过，3 项 Windows 专属跳过。
- `python3 -m compileall -q scripts tests`、Skill `quick_validate.py`、`git diff --check`、500 行文件上限与 100 行函数上限检查全部通过。
