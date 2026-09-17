# 演示数据

`demo_data.json` 中的频道、群组、人员、用户名、消息、标识符和指标均为虚构数据，只用于本地功能演示和测试。

演示导入由 `python -m app.demo_seed` 完成，并且只允许在 `DEMO_MODE=1` 时运行。演示模式不启动 Telegram worker、Bot 或 AI，不会向外部服务提交扫描或导出任务。
