此目录只应保存部署者在本机创建的 Secret 文件。
不要把本目录上传到聊天、Git、Issue、构建上下文或公共网盘。

setup.sh 或 demo-start.sh 会创建 Compose 所需文件。文件名包括：
- platform_bootstrap_password
- telegram_api_hash
- telegram_database_passphrase
- telegram_standby_database_passphrase（可选，空文件表示未配置）
- telegram_bot_token（可选）
- ai_api_key（可选）
- database_password（外部 PostgreSQL 可选）
- site_proxy_token（受保护的 /api/site 接口可选）

公开源码包只保留本说明，不包含上述文件。
