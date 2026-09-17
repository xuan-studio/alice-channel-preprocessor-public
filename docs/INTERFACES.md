# 系统接口说明

> 轩制作 · Made by XUAN · [Article](https://x.com/Yuanzhuo_labs/status/2099855596728328701)

## 输入

### 频道研究任务

- 单个 Telegram `@username`。
- `https://t.me/<username>` 公开链接。
- 包含多个 username/链接的自由文本，系统会去重并报告无效项。
- 可选任务标签和管理员强制重扫标志。

私密邀请链接不会被自动加入，会进入人工复核或返回明确错误。

### 聊天历史导出

- 当前导出账号可访问的群组、频道 username、公开链接或支持的内部 chat 标识。
- 日期范围：最近 7 天、30 天、全部或自定义。
- 最大消息数和是否包含媒体。

### 维护输入

- 联系人/互动用户的跟进状态、标签和备注。
- 频道标签和人工语种纠正。
- 管理员创建的成员、邀请和临时密码。

## 输出

- 频道/群组研究卡片和详情页。
- 频道元数据、活动时间、近期内容和置顶证据。
- 联系方式证据及实体类型：用户、Bot、频道、群组或未知。
- 评论/群消息中的公开 username 排行。
- 语种、浏览、反应、回复、转发、更新频率和互动率。
- 群组质量判断及证据摘要。
- Telegram 官方相似频道发现池。
- 单任务 CSV/JSON 和全局线索 CSV。
- 聊天历史 CSV/JSONL；启用媒体时可生成归档。

## Web 与 API

### 浏览器界面

所有管理页面默认要求登录，并使用 session Cookie 与 CSRF Token。主要页面：

| 路径 | 用途 |
| --- | --- |
| `/` | 任务台与批量提交 |
| `/jobs/{id}` | 任务详情、证据、标签和线索维护 |
| `/leads` | 跨任务全局线索池 |
| `/discover` | 相似频道发现池 |
| `/chat-exports` | 聊天历史导出任务 |
| `/admin/users` | 管理员成员与邀请管理 |
| `/health` | 进程健康检查，不包含敏感数据 |
| `/ready` | 数据库、Telegram 与 AI 配置就绪状态 |

### 会话 API

- `POST /api/scans`：创建单个扫描任务，需要已登录 session 和 `X-CSRF-Token`。
- `GET /api/scans/{id}`：读取任务状态和摘要。

### 预留站点 API

仓库保留现有 `/api/site/*` 接口，供未来受控前端或其他系统使用。本次公开整理没有新增跨系统集成。

该接口采用两层认证：

1. `X-Alice-Proxy-Token` 必须匹配 `SITE_PROXY_TOKEN`；
2. 登录后取得 Bearer session token 和 CSRF token。

主要端点包括：

- `POST /api/site/session`、`DELETE /api/site/session`
- `GET/POST /api/site/jobs`
- `POST /api/site/jobs/preview`
- `POST /api/site/jobs/batch`
- `GET /api/site/jobs/{id}`
- `PUT /api/site/jobs/{id}/tags`
- `PUT /api/site/jobs/{id}/contacts`
- `PUT /api/site/jobs/{id}/commenters/{commenter_id}`
- `GET /api/site/jobs/{id}/exports/{kind}`

不要把 Proxy Token 放进浏览器前端或公开仓库。未配置时，这组接口拒绝访问。

## 导入与导出格式

### 批量目标输入

网页接受换行文本，不要求专用文件格式。每个文本可包含 username、公开链接和说明文字。

### 演示数据

`demo/demo_data.json` 是公开的最小数据模板，结构包括：

- `channel`：频道快照；
- `commenters`：互动用户；
- `contacts`：公开联系方式证据；
- `raw_artifacts`：置顶/帖子样本；
- `summary`：语种、互动、质量、相似频道和总结。

它只用于演示，不是面向不可信来源的通用导入 API。真实业务数据应通过扫描流程产生，或在增加验证、权限和审计后再开发专用导入器。

### CSV/JSON

导出字段以页面下载结果为准，包含任务、频道、联系人、互动用户、状态、标签和证据来源。聊天历史 JSONL 每行一条消息，CSV 用于表格处理。

## 人工审批与接手

- Telegram 登录验证码和两步验证必须由账号持有人处理。
- 私密邀请、无权访问和 `CHANNEL_PRIVATE` 目标需要人工判断。
- 管理员强制重扫、成员管理和密码重置由有权限的后台用户执行。
- 公开联系人只是研究线索；实际联系、报价、承诺和发送消息必须由人员在系统外完成。
- AI 输出是辅助分析，不替代证据核验或业务审批。

## 演示模式

`DEMO_MODE=1` 时：

- Web 仍要求登录；
- 可以查看、筛选、导出和维护虚构本地数据；
- 扫描、相似频道入队和聊天导出创建会被拒绝；
- Docker 默认只启动 Web，不启动 worker 或 Bot；
- 不调用 Telegram 或 AI。

## 后续系统连接

未来可通过受控的 `/api/site/*` 或离线 CSV/JSON 与其他系统连接。接入前应单独设计：认证、字段映射、数据最小化、审批、幂等、重试、审计和删除策略。本次整理不创建任何跨系统数据流。
