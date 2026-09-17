# 第三方组件与许可说明

本文件用于发布前审阅，不替代各上游项目的正式许可证文本。版本以 `requirements.txt`、`requirements-dev.txt` 和 `Dockerfile` 为准。

## 直接依赖

| 组件 | 固定版本/来源 | 上游许可 | 用途 |
| --- | --- | --- | --- |
| FastAPI | 0.116.1 | MIT | Web API 与路由 |
| Uvicorn | 0.35.0 | BSD-3-Clause | ASGI 服务 |
| SQLAlchemy | 2.0.43 | MIT | ORM 与数据库访问 |
| psycopg / psycopg-binary | 3.2.9 | LGPL-3.0 | PostgreSQL 驱动 |
| httpx | 0.28.1 | BSD-3-Clause | Telegram Bot/AI HTTP 客户端 |
| Jinja2 | 3.1.6 | BSD-3-Clause | 页面模板 |
| argon2-cffi | 25.1.0 | MIT | 密码哈希 |
| python-multipart | 0.0.20 | Apache-2.0 | 表单解析 |
| pytest | 8.4.1 | MIT | 开发测试 |
| TDLib | commit `d1085f9cebc5a62379991ae1652673954f229c1f` | Boost Software License 1.0 | Telegram 客户端协议库 |

上游入口：

- <https://github.com/fastapi/fastapi>
- <https://github.com/encode/uvicorn>
- <https://github.com/sqlalchemy/sqlalchemy>
- <https://github.com/psycopg/psycopg>
- <https://github.com/encode/httpx>
- <https://github.com/pallets/jinja>
- <https://github.com/hynek/argon2-cffi>
- <https://github.com/Kludex/python-multipart>
- <https://github.com/pytest-dev/pytest>
- <https://github.com/tdlib/td>

Docker 构建会从上游源码编译固定 commit 的 TDLib；源码包本身不包含 `libtdjson` 二进制。构建和分发镜像时仍需遵守 TDLib 及镜像中全部传递依赖的许可与通知要求。

## 传递依赖与基础镜像

Python 包会带入 Starlette、Pydantic、AnyIO 等传递依赖；Ubuntu 基础镜像和 apt 安装的软件也各自适用上游许可。正式发布镜像前，建议对最终 SBOM/镜像执行一次许可证扫描，而不是只依赖本表。

## 素材

当前页面不捆绑第三方图片、字体、图标包或演示截图。演示数据为本项目专用的虚构文本数据。

## 项目自身许可证

当前源码副本没有 `LICENSE` 文件，作者尚未选择项目级许可证。这是公开发布前必须决定的事项。第三方组件的开源许可不会自动授予本项目自身代码的使用权，也不能把第三方代码标记为 XUAN 原创。

加入项目许可证时，应保留本文件和上游要求的版权、许可及署名，并确认所选许可证与 LGPL 依赖、容器分发方式及预期商业使用方式兼容。必要时请咨询专业法律顾问。
