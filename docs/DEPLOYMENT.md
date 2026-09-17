# 部署文档

> 轩制作 · Made by XUAN · [Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)  
> 本系统是上述 BD 方法文章的配套项目。

## 1. 运行要求

- 64 位 Linux、macOS 或支持 Linux 容器的 Windows。
- Docker Engine 24+ 或较新的 Docker Desktop。
- Docker Compose v2，命令为 `docker compose`。
- 首次构建需要访问 Ubuntu 软件源、GitHub 和 Python 包索引。
- 建议至少 2 CPU、4 GB 内存和 10 GB 可用磁盘；编译 TDLib 会消耗时间和内存。

真实扫描还需要：

- 部署者自己的 Telegram 账号。
- 在 <https://my.telegram.org/apps> 创建的 `api_id` 和 `api_hash`。
- 可选的 OpenAI-compatible API；启用后可能收费。
- 可选的 Telegram Bot Token；只有明确配置 allowlist 后才应启用。

## 2. 从全新源码启动演示

演示模式不需要 Telegram 或 AI 凭据，也不会启动采集 worker。

```bash
unzip alice-channel-preprocessor-public.zip
cd alice-channel-preprocessor-public
./scripts/demo-start.sh
```

终端会输出本机 URL、`demo_admin` 用户名和随机生成的密码。登录后可验证：

- 首页显示两个“虚构演示”目标；
- 详情页显示语种、互动指标、联系人身份和证据；
- 线索池显示虚构联系人和互动用户；
- 发现池显示虚构相似频道；
- 尝试创建扫描任务会显示演示模式只读提示。

健康检查：

```bash
curl -fsS http://127.0.0.1:8858/health
```

预期返回包含 `"ok":true` 和当前服务版本的 JSON。

## 3. 配置真实部署

不要从演示目录直接覆盖真实配置。建议重新解压一份干净源码，然后运行：

```bash
./scripts/setup.sh
```

脚本会交互式创建：

- `.env`：非敏感运行配置；
- `secrets/platform_bootstrap_password`：后台管理员密码；
- `secrets/telegram_api_hash`：Telegram API Hash；
- `secrets/telegram_database_passphrase`：TDLib 本地数据库加密口令；
- 可选的 `secrets/ai_api_key` 和 `secrets/telegram_bot_token`。

`TELEGRAM_API_ID` 放在 `.env`，其余秘密通过 Docker secrets 文件挂载。不要提交生成后的 `.env` 或 `secrets/` 内容。

## 4. 登录 Telegram 采集账号

```bash
./scripts/login-collector.sh
```

TDLib 会要求手机号、验证码，以及账号启用两步验证时的密码。该步骤需要人员在终端接手。登录状态保存在 Docker volume 中，不在源码目录或分享压缩包中。

不要复制其他人的 TDLib session，也不要把 Docker volume 导出到公开位置。

## 5. 启动与验证真实服务

```bash
./scripts/preflight.sh
./scripts/start.sh
./scripts/status.sh
```

默认地址为 <http://127.0.0.1:8848>。第一次登录使用初始化管理员账号。建议登录后立即创建个人管理员并轮换初始化密码。

真实流程验证建议只使用你有权访问的测试频道：

1. 提交一个公开 Telegram username；
2. 等待任务到 `completed` 或明确的失败状态；
3. 检查频道资料、公开联系人证据和互动指标；
4. 导出一次任务 CSV/JSON；
5. 不要在验收时向任何联系人发送消息。

## 6. 配置说明

公开模板为 `.env.example`。主要变量：

| 变量 | 用途 | 默认/要求 |
| --- | --- | --- |
| `DEMO_MODE` | 只读虚构数据模式 | 真实部署必须为 `0` |
| `TELEGRAM_WORKER_ENABLED` | 就绪检查中的 worker 标志 | 真实部署为 `1` |
| `PUBLIC_BASE_URL` | 页面和通知中的外部 URL | 本机默认为 HTTP |
| `TRUSTED_HOSTS` | 允许的 Host | 生产环境禁止 `*` |
| `SESSION_COOKIE_SECURE` | 仅 HTTPS 发送 Cookie | 生产环境必须为 `1` |
| `PLATFORM_BOOTSTRAP_USERNAME` | 初始管理员用户名 | 默认 `admin` |
| `TELEGRAM_API_ID` | Telegram 应用 ID | 真实扫描必填 |
| `AI_ENABLED` | AI 总结开关 | 默认关闭 |
| `BOT_ENABLED` | 工作群 Bot 开关 | 默认关闭 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Bot 允许的群 | 启用 Bot 时必填 |
| `TELEGRAM_ALLOWED_USER_IDS` | Bot 允许的用户 | 强烈建议填写 |
| `DATABASE_URL` | 可选的完整数据库 URL | Compose 默认 SQLite |
| `DATABASE_HOST/USER/NAME/PORT` | 外部 PostgreSQL 分项配置 | 生产环境使用 |

高级变量及全部字段见 `.env.example` 和 `app/config.py`。带 `_FILE` 的秘密变量应指向容器内 secret 文件。

## 7. 生产环境最低要求

把 `APP_ENV` 设置为 `production` 后，应用会强制检查：

- 至少 16 位管理员密码；
- PostgreSQL，不允许 SQLite；
- HTTPS 的 `PUBLIC_BASE_URL`；
- `SESSION_COOKIE_SECURE=1`；
- 明确的 `TRUSTED_HOSTS`，不允许通配符。

本源码包不捆绑域名、反向代理、证书或云厂商配置。可在宿主机使用 Caddy、Nginx、Traefik 或云负载均衡器终止 HTTPS。不要把管理后台直接暴露在无 TLS 的公网。

## 8. AI 与费用

AI 默认关闭。启用前：

1. 将 Key 写入 `secrets/ai_api_key`；
2. 在 `.env` 设置 `AI_ENABLED=1`；
3. 设置兼容的 `AI_BASE_URL` 和模型名；
4. 确认供应商的数据处理政策、访问权限和费用。

系统只在显式启用后调用 AI。规则提取和本地指标不依赖 AI。

## 9. Bot 与审批边界

Bot 默认关闭。启用时必须设置允许的群 ID；建议同时设置允许的用户 ID。Bot 只能创建研究任务、查询状态和发送任务结果，不应被视为对外触达工具。

以下事项需要人员处理：

- Telegram 首次登录、验证码和两步验证；
- 私密邀请链接或账号无权访问的目标；
- 生产凭据、域名、HTTPS、数据库和备份策略；
- 对外联系、报价、合作承诺和客户沟通；
- 项目级开源许可证选择。

## 10. 保存数据与停止服务

创建 SQLite 备份：

```bash
./scripts/backup.sh
```

备份会写入本机 `backups/`，该目录默认不进入 Git 或源码压缩包。生产 PostgreSQL 请使用数据库厂商的备份工具。

停止服务但保留 volumes：

```bash
./scripts/stop.sh
```

不要运行 `docker compose down -v`，除非你明确要删除数据库和 Telegram 登录状态。

## 11. 常见错误

### `缺少 Docker Compose v2`

确认 `docker compose version` 可运行，而不是旧版 `docker-compose`。

### Docker 构建在 TDLib 阶段很慢

TDLib 会从固定 commit 编译。确保网络可访问 GitHub、内存足够，并保留 Docker build cache。

### `缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH`

运行 `./scripts/setup.sh`，并确认 `secrets/telegram_api_hash` 非空。公开包不会内置测试或共享凭据。

### Telegram 登录后 worker 仍不可用

确认使用同一个 `TELEGRAM_ACCOUNT_NAME` 和数据库加密口令；不要随意更换已有 session 的 passphrase。

### `CHANNEL_PRIVATE`

当前账号无权读取目标。系统会保留旧结果或标记访问受限，不会绕过 Telegram 权限。

### 登录页面打不开

运行 `./scripts/status.sh`，检查端口是否冲突，再读取 `docker compose logs web`。演示端口为 `8858`，真实模式默认为 `8848`。

### 演示脚本提示已经存在 `.env`

这是防止覆盖真实配置的保护。请在新的源码副本运行演示，不要删除你仍需使用的 `.env`。

## 12. 作者与原文

轩制作 · Made by XUAN

[Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)

基础部署步骤均在本文档中。联系作者用于进一步交流部署实践、业务适配、合作与 BD 人才对接。

## 公开版本验证环境

本次测试使用 Python 3.12；Python 3.9 不受支持。81 项测试通过，真实 Telegram/AI 和完整 Docker 构建未在本次验证。详见 [验证记录](../PUBLICATION.md)。
