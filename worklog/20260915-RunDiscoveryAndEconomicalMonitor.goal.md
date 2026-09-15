# Run 可发现性与经济监护改进

## 预期目标

- 批判性审查 `/tmp/suggestion2.txt`，采纳适合少量自用场景的高价值改进。
- 让后台 Run 在执行期间可持久发现和查询，不改变现有一个任务一个 worker 的执行模型。
- 让经济监护可明确配置成功/失败审查范围，避免无需审查的成功任务调用 Luna。
- 将真实监护结论随完成消息送达，并保持失败显式可见。

## 行进状态

- 已完成：收敛后的可发现性、经济监护和摘要送达均已实现并验证。

## 最终已交付成果

- 每个新 Run 持久化不可变 spec、单写者 runtime 与 `${CODEX_HOME:-~/.codex}/wake-run/index/` 全局索引。
- 新增 `--name`、`--list [--active]` 和 `--show <run_id>`，查询返回运行状态、PID 存活、日志元数据、监护、投递与 Goal Guard 信息。
- monitor schema v2 新增 `review_on`；schema v1 保持成功/失败都审查的原语义。
- 监护 policy 与 runtime 分离，session 在首个被选中的结果上懒创建；failure-only 成功路径不调用 Luna。
- 唤醒消息携带最终真实审查的动作、分类、摘要和原因；成功重试后跳过审查时不会误用先前失败摘要。
- 保留一个 Run 一个阻塞式 worker、JSON/原子写和既有 completion/replay 模型；未引入动态 policy loop、控制通道、daemon、数据库或进程接管。
- README、中文 README、Skill 与经济监护 reference 已同步新契约。
- 最终验证：101 项测试通过、3 项 Windows 专属跳过；Python 编译、Skill validator、diff 检查及文件/函数长度指标通过。
