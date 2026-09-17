# Telegram 频道预处理台

> 轩制作 · Made by XUAN  
> [Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)

这是 BD 方法书配套的独立系统，用于把公开 Telegram 频道和群组整理成可复核的研究卡片、联系人证据、互动用户线索和相似频道发现池。它适合需要批量筛选公开社群、维护合作线索和保留研究依据的 BD、增长与社区团队。

本项目只处理公开信息。它不包含自动私信、群发、拉人、自动评论或绕过 Telegram 权限的能力。

## 已完成功能

- 批量识别公开 `@username`、`t.me` 链接和混合文本中的目标。
- 使用 TDLib 读取频道/群组资料、近期内容、置顶消息和可访问互动。
- 计算主要语种、浏览/反应/回复/转发、更新频率和活跃度指标。
- 区分公开联系人中的用户、Bot、频道和群组，避免把非用户实体当负责人。
- 汇总评论用户、联系人证据、来源频道、标签、跟进状态和备注。
- 读取 Telegram 官方相似频道推荐，并建立去重后的频道发现池。
- 对公开群组给出真实讨论、混合内容、纯广告群等质量判断。
- 提供 CSV/JSON 导出、聊天历史导出任务、账户角色、审计和任务生命周期。
- AI 为可选增强；未配置时仍保留规则提取和本地指标。
- 提供完全虚构的本地演示模式，不启动 Telegram、Bot 或 AI。

## 尚未完成与限制

- 真实扫描必须使用部署者自己的 Telegram API 凭据和已登录账号。
- 私有、已封禁或当前账号无权访问的目标无法扫描，系统不会绕过权限。
- 默认 SQLite 与单 worker 适合单机或小团队；生产环境应改用外部 PostgreSQL，并自行配置 HTTPS、备份和监控。
- AI 接口兼容 OpenAI Chat Completions 格式，但不同供应商需要自行验证；启用后可能产生费用。
- 聊天历史导出只覆盖当前 Telegram 账号有权访问的内容，媒体导出可能占用大量存储。
- 当前仓库**没有项目级许可证**。在作者明确选择并加入 `LICENSE` 前，不应把“可查看源码”表述为已经正式开源，也不能推定获得复制、修改或再分发授权。

## 快速体验

要求：Docker Engine 或 Docker Desktop，以及 Docker Compose v2。

```bash
./scripts/demo-start.sh
```

脚本会：

1. 生成仅用于本机的随机管理员密码；
2. 构建并启动 Web 服务，不启动采集 worker；
3. 导入 `demo/demo_data.json` 中的虚构频道、群组、联系人和推荐；
4. 在终端显示登录地址、用户名和一次性生成的本地密码。

默认访问地址：<http://127.0.0.1:8858>

演示模式需要登录，并拒绝创建 Telegram 扫描、AI 请求和聊天导出任务。演示数据中的名称、账号、消息、标识符和指标均为虚构内容。

## 真实部署

完整步骤见 [部署文档](docs/DEPLOYMENT.md)。最短路径是：

```bash
./scripts/setup.sh
./scripts/login-collector.sh
./scripts/start.sh
./scripts/status.sh
```

`setup.sh` 会交互式创建 `.env` 和本地 `secrets/` 文件。项目不会提供 Telegram、AI 或 Bot 的共享凭据。

## 文档

- [部署、配置、备份与排错](docs/DEPLOYMENT.md)
- [输入、输出、导入导出与 API](docs/INTERFACES.md)
- [演示数据说明](demo/README.md)
- [第三方组件和许可注意事项](THIRD-PARTY.md)
- [更新记录](CHANGELOG.md)

## 目录

```text
app/          FastAPI、数据模型、任务仓库、TDLib 与 AI 逻辑
templates/    服务端页面模板
static/       页面样式
vendor/       项目使用的 TDLib Python 运行封装
demo/         明确标注的虚构演示数据
scripts/      初始化、登录、启动、停止、备份和演示脚本
tests/        自动化测试
docs/         部署与接口文档
```

运行时生成的 `.env`、`secrets/`、数据库、Telegram session、导出、日志和备份均被忽略，也不应放入分享压缩包。

## 安全边界

- Web 后台默认需要账户登录，并使用 CSRF 校验。
- 演示服务默认只绑定 `127.0.0.1`。
- Bot 默认关闭；启用时必须设置群和用户 allowlist。
- AI 默认关闭；启用前请确认数据处理政策、费用和供应商条款。
- 对外联系与业务审批不在本系统内自动执行，必须由人员在其他渠道完成。

## 作者与交流

轩制作 · Made by XUAN

原文入口：[Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)

这套系统是 BD 方法书的一部分，会持续更新。欢迎联系作者交流部署实践、业务合作和最新使用经验。你是正在找机会的 BD，或者正在招聘 BD 的老板，也欢迎联系。

联系方式：[X / 私信](https://x.com/Yuanzhuo_labs) · 微信：`zhe_eth` · Telegram：`@xuan_I123`

基础部署步骤已完整公开在本仓库，不需要通过私信索取。

## BD 方法书与其他系统

[书籍主页](https://github.com/xuan-studio/bd-playbook) · [系统目录](https://github.com/xuan-studio/bd-playbook/tree/main/systems) · [原文 Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)

各系统独立部署，目前没有自动打通数据。请先阅读各项目的接口说明，再决定如何衔接。欢迎交流部署实践、业务合作、BD 求职与招聘。

[本次验证范围](PUBLICATION.md)
