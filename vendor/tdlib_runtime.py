from __future__ import annotations

import ctypes
import base64
import fcntl
import getpass
import hashlib
import json
import os
import platform
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROJECT_LOCAL_LIBRARY_PATH = (
    Path(__file__).resolve().parents[1] / "tdlib-latest-install" / "lib" / "libtdjson.dylib"
)

DEFAULT_LIBRARY_PATHS = (
    str(PROJECT_LOCAL_LIBRARY_PATH),
    "/opt/homebrew/opt/tdlib/lib/libtdjson.dylib",
    "/opt/homebrew/lib/libtdjson.dylib",
    "/usr/local/lib/libtdjson.dylib",
    "/usr/local/lib/libtdjson.so",
    "/opt/tdlib/lib/libtdjson.so",
)

class TdlibError(RuntimeError):
    pass


LONG_NUMBER_RE = re.compile(r"(?<!\w)\+?\d{7,}(?!\w)")


def redact_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return LONG_NUMBER_RE.sub("<redacted-number>", text)


def derive_database_encryption_key(passphrase: str, account_name: str) -> str:
    if len(passphrase) < 8:
        raise TdlibError("本地数据库口令至少需要 8 个字符。")
    digest = hashlib.sha256(
        passphrase.encode("utf-8") + b"|standalone-tdlib-cli|" + account_name.encode("utf-8")
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def public_username(entity: dict[str, Any]) -> str:
    usernames = entity.get("usernames") or {}
    active_usernames = usernames.get("active_usernames") or [] if isinstance(usernames, dict) else []
    if active_usernames:
        return str(active_usernames[0] or "")
    return str(entity.get("username", "") or "")


def sanitized_user(user: dict[str, Any]) -> dict[str, Any]:
    username = public_username(user)
    user_type = user.get("type") or {}
    return {
        "user_id": int(user.get("id", 0) or 0),
        "first_name": str(user.get("first_name", "") or ""),
        "last_name": str(user.get("last_name", "") or ""),
        "username": username,
        "is_bot": str(user_type.get("@type", "")) == "userTypeBot" if isinstance(user_type, dict) else False,
        "is_premium": bool(user.get("is_premium", False)),
        "is_verified": bool(user.get("is_verified", False)),
    }


def reaction_type_label(reaction_type: dict[str, Any] | None) -> str:
    value = reaction_type or {}
    kind = str(value.get("@type", "") or "")
    if kind == "reactionTypeEmoji":
        return str(value.get("emoji", "") or "")
    if kind == "reactionTypeCustomEmoji":
        return f"custom:{int(value.get('custom_emoji_id', 0) or 0)}"
    if kind == "reactionTypePaid":
        return "paid"
    return kind or "unknown"


def _interactive_value(env_name: str, prompt: str, *, secret: bool) -> str:
    value = str(os.getenv(env_name, "") or "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise TdlibError(f"{prompt.strip()} 需要在交互式 Terminal 中输入。")
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def prompt_runtime_credentials(
    account_name: str,
    *,
    include_phone: bool,
    use_official_test_credentials: bool = False,
) -> dict[str, Any]:
    if use_official_test_credentials:
        raise TdlibError("公开源码包不内置 Telegram 测试凭据，请配置自己的 TELEGRAM_API_ID/API_HASH。")
    api_id_text = _interactive_value("TELEGRAM_API_ID", "Telegram API ID: ", secret=False)
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise TdlibError("Telegram API ID 必须是整数。") from exc
    if api_id <= 0:
        raise TdlibError("Telegram API ID 必须大于 0。")
    api_hash = _interactive_value("TELEGRAM_API_HASH", "Telegram API Hash（隐藏输入）: ", secret=True)
    if not api_hash:
        raise TdlibError("Telegram API Hash 不能为空。")
    database_passphrase = _interactive_value(
        "TDLIB_DATABASE_PASSPHRASE",
        f"{account_name} 的本地数据库口令（隐藏输入，至少 8 位）: ",
        secret=True,
    )
    phone = ""
    if include_phone:
        phone = _interactive_value("TELEGRAM_PHONE", "Telegram 手机号（隐藏输入，含国家码）: ", secret=True)
        if not phone:
            raise TdlibError("Telegram 手机号不能为空。")
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "database_encryption_key": derive_database_encryption_key(database_passphrase, account_name),
        "phone": phone,
    }


def resolve_library_path(explicit: str | None = None) -> Path:
    candidates = [explicit, os.getenv("TDLIB_JSON_LIBRARY"), *DEFAULT_LIBRARY_PATHS]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    raise TdlibError("没有找到 libtdjson。请安装 TDLib 或设置 TDLIB_JSON_LIBRARY。")


class TdjsonClient:
    def __init__(self, library_path: str | None = None):
        self.library_path = resolve_library_path(library_path)
        self._library = ctypes.CDLL(str(self.library_path))
        self._configure_library()
        self._library.td_set_log_verbosity_level(0)
        self._client = self._library.td_json_client_create()
        if not self._client:
            raise TdlibError("TDLib client 创建失败。")

    def _configure_library(self) -> None:
        self._library.td_json_client_create.restype = ctypes.c_void_p
        self._library.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self._library.td_json_client_receive.restype = ctypes.c_void_p
        self._library.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_execute.restype = ctypes.c_void_p
        self._library.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        self._library.td_set_log_verbosity_level.argtypes = [ctypes.c_int]

    def set_log_verbosity(self, level: int = 0) -> None:
        self._library.td_set_log_verbosity_level(int(level))

    def send(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._library.td_json_client_send(self._client, raw)

    def receive(self, timeout: float = 0.2) -> dict[str, Any] | None:
        raw = self._library.td_json_client_receive(self._client, float(timeout))
        if not raw:
            return None
        value = ctypes.cast(raw, ctypes.c_char_p).value
        return json.loads(value.decode("utf-8")) if value else None

    def close(self) -> None:
        if self._client:
            self._library.td_json_client_destroy(self._client)
            self._client = None

    def __enter__(self) -> "TdjsonClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class TdlibAccountSession:
    def __init__(
        self,
        *,
        account_name: str,
        account_dir: Path,
        api_id: int,
        api_hash: str,
        database_encryption_key: str,
        library_path: str | None = None,
        proxy: dict[str, Any] | None = None,
        request_timeout: float = 30.0,
    ):
        self.account_name = account_name
        self.account_dir = account_dir.resolve()
        self.database_dir = self.account_dir / "database"
        self.files_dir = self.account_dir / "files"
        self.database_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.database_encryption_key = database_encryption_key
        self.proxy = dict(proxy or {})
        self._proxy_id = 0
        self._proxy_applied = False
        self.request_timeout = request_timeout
        self.client = TdjsonClient(library_path)
        self.client.set_log_verbosity(0)
        self._request_counter = 0

    def close(self) -> None:
        try:
            self.client.send({"@type": "close"})
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                update = self.client.receive(0.1)
                state = (update or {}).get("authorization_state") or {}
                if (update or {}).get("@type") == "updateAuthorizationState" and state.get("@type") == "authorizationStateClosed":
                    break
        except Exception:
            pass
        self.client.close()

    def __enter__(self) -> "TdlibAccountSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def invoke(self, method: str, **params: Any) -> dict[str, Any]:
        self._request_counter += 1
        extra = f"request-{self._request_counter}"
        self.client.send({"@type": method, "@extra": extra, **params})
        deadline = time.monotonic() + self.request_timeout
        while time.monotonic() < deadline:
            response = self.client.receive(min(0.25, max(0.01, deadline - time.monotonic())))
            if not response or response.get("@extra") != extra:
                continue
            if response.get("@type") == "error":
                code = int(response.get("code", 0) or 0)
                message = redact_text(response.get("message", "TDLib 请求失败。"), (self.api_hash,))
                raise TdlibError(f"{method} 失败（{code}）：{message}")
            return response
        raise TdlibError(f"TDLib 请求超时：{method}")

    def authorization_state(self) -> dict[str, Any]:
        return self.invoke("getAuthorizationState")

    def bootstrap(self) -> dict[str, Any]:
        state = self.authorization_state()
        if state.get("@type") == "authorizationStateWaitTdlibParameters":
            common_parameters = {
                "use_test_dc": False,
                "database_directory": str(self.database_dir),
                "files_directory": str(self.files_dir),
                "use_file_database": True,
                "use_chat_info_database": True,
                "use_message_database": True,
                "use_secret_chats": False,
                "api_id": self.api_id,
                "api_hash": self.api_hash,
                "system_language_code": "zh-hans",
                "device_model": "Standalone TDLib CLI",
                "system_version": platform.platform(),
                "application_version": "0.2.0",
            }
            try:
                # Current TDLib exposes setTdlibParameters as flat fields.
                self.invoke(
                    "setTdlibParameters",
                    **common_parameters,
                    database_encryption_key=self.database_encryption_key,
                )
            except TdlibError as exc:
                if "Parameters aren't specified" not in str(exc):
                    raise
                # TDLib 1.8.0 Homebrew schema wraps fields in tdlibParameters
                # and asks for the database key in a following state.
                self.invoke(
                    "setTdlibParameters",
                    parameters={
                        "@type": "tdlibParameters",
                        **common_parameters,
                        "enable_storage_optimizer": True,
                        "ignore_file_names": False,
                    },
                )
            state = self.authorization_state()
        if state.get("@type") == "authorizationStateWaitEncryptionKey":
            self.invoke("checkDatabaseEncryptionKey", encryption_key=self.database_encryption_key)
            state = self.authorization_state()
        if self.proxy and not self._proxy_applied:
            # Apply the proxy after the local database has been opened.  TDLib
            # permits proxy calls before authorization, but doing this after the
            # encryption-key transition makes the persisted proxy state
            # deterministic for both fresh and returning sessions.
            self._apply_proxy()
        return state

    def _apply_proxy(self) -> None:
        proxy_type = str(self.proxy.get("type", "") or "socks5").strip().lower()
        host = str(self.proxy.get("host", "") or "").strip()
        port = int(self.proxy.get("port", 0) or 0)
        if proxy_type != "socks5" or host not in {"127.0.0.1", "::1", "localhost"} or port <= 0:
            raise TdlibError("本机安全模式只允许有效的 localhost SOCKS5 代理。")
        result = self.invoke(
            "addProxy",
            proxy={
                "@type": "proxy",
                "server": host,
                "port": port,
                "type": {"@type": "proxyTypeSocks5", "username": "", "password": ""},
            },
            enable=True,
        )
        self._proxy_id = int((result.get("proxy") or {}).get("id", 0) or result.get("id", 0) or 0)
        if self._proxy_id <= 0:
            raise TdlibError("TDLib 没有确认 SOCKS5 代理绑定；已停止，禁止回落直连。")
        # `enable=True` is part of addProxy, but explicitly enabling and reading
        # the registered proxy back gives us a fail-closed proof compatible
        # with the current AddedProxy schema (network fields are nested under
        # `proxy`).
        self.invoke("enableProxy", proxy_id=self._proxy_id)
        registered = self.invoke("getProxies")
        matched = False
        for item in list(registered.get("proxies") or []):
            network = item.get("proxy") or {}
            if (
                int(item.get("id", 0) or 0) == self._proxy_id
                and bool(item.get("is_enabled"))
                and str(network.get("server", "") or "") in {"127.0.0.1", "::1", "localhost"}
                and int(network.get("port", 0) or 0) == port
            ):
                matched = True
                break
        if not matched:
            raise TdlibError("TDLib 未确认绑定的 SOCKS5 代理处于启用状态；已停止，禁止回落直连。")
        self._proxy_applied = True

    def login(self, phone: str) -> dict[str, Any]:
        state = self.bootstrap()
        phone_submitted = False
        code_attempts = 0
        password_attempts = 0
        while True:
            state_type = str(state.get("@type", ""))
            if state_type == "authorizationStateReady":
                return sanitized_user(self.invoke("getMe"))
            if state_type == "authorizationStateWaitPhoneNumber":
                if phone_submitted:
                    raise TdlibError("手机号未被接受，请检查号码、API 凭据或网络。")
                self.invoke(
                    "setAuthenticationPhoneNumber",
                    phone_number=phone,
                    settings={
                        "@type": "phoneNumberAuthenticationSettings",
                        "allow_flash_call": False,
                        "allow_missed_call": False,
                        "is_current_phone_number": False,
                        "allow_sms_retriever_api": False,
                    },
                )
                phone_submitted = True
            elif state_type == "authorizationStateWaitCode":
                code_attempts += 1
                if code_attempts > 3:
                    raise TdlibError("验证码连续失败，已停止本次登录。")
                code = _interactive_value("TELEGRAM_LOGIN_CODE", "Telegram 验证码（隐藏输入）: ", secret=True)
                try:
                    self.invoke("checkAuthenticationCode", code=code)
                except TdlibError:
                    if code_attempts >= 3:
                        raise
            elif state_type == "authorizationStateWaitPassword":
                password_attempts += 1
                if password_attempts > 3:
                    raise TdlibError("2FA 连续失败，已停止本次登录。")
                password = _interactive_value("TELEGRAM_2FA_PASSWORD", "Telegram 2FA 密码（隐藏输入）: ", secret=True)
                try:
                    self.invoke("checkAuthenticationPassword", password=password)
                except TdlibError:
                    if password_attempts >= 3:
                        raise
            elif state_type == "authorizationStateWaitRegistration":
                raise TdlibError("该号码需要注册新 Telegram 账号；本验证器不会自动注册。")
            elif state_type == "authorizationStateWaitOtherDeviceConfirmation":
                raise TdlibError("当前需要其他设备确认；本版本尚未加入二维码登录。")
            elif state_type in {"authorizationStateClosing", "authorizationStateClosed"}:
                raise TdlibError("TDLib 会话已关闭。")
            elif state_type:
                raise TdlibError(f"暂不支持的授权状态：{state_type}")
            state = self.authorization_state()

    def get_me(self) -> dict[str, Any]:
        state = self.bootstrap()
        if state.get("@type") != "authorizationStateReady":
            raise TdlibError("该账号槽位尚未登录；请先运行 accounts login。")
        return sanitized_user(self.invoke("getMe"))

    def _sender_record(self, sender: dict[str, Any]) -> dict[str, Any]:
        sender_type = str(sender.get("@type", "") or "")
        if sender_type == "messageSenderUser":
            user_id = int(sender.get("user_id", 0) or 0)
            try:
                user = sanitized_user(self.invoke("getUser", user_id=user_id))
                display_name = " ".join(
                    part for part in (user.get("first_name", ""), user.get("last_name", "")) if part
                ).strip()
                username = str(user.get("username", "") or "")
                return {
                    "sender_type": "user",
                    "user_id": user_id,
                    "sender_chat_id": 0,
                    "username": username,
                    "display_name": display_name,
                    "missing_username_reason": "" if username else "no_public_username",
                }
            except TdlibError:
                return {
                    "sender_type": "user",
                    "user_id": user_id,
                    "sender_chat_id": 0,
                    "username": "",
                    "display_name": "",
                    "missing_username_reason": "user_unavailable",
                }
        if sender_type == "messageSenderChat":
            sender_chat_id = int(sender.get("chat_id", 0) or 0)
            title = ""
            username = ""
            try:
                chat = self.invoke("getChat", chat_id=sender_chat_id)
                title = str(chat.get("title", "") or "")
                chat_type = chat.get("type") or {}
                if chat_type.get("@type") == "chatTypeSupergroup":
                    supergroup = self.invoke(
                        "getSupergroup", supergroup_id=int(chat_type.get("supergroup_id", 0) or 0)
                    )
                    username = public_username(supergroup)
            except TdlibError:
                pass
            return {
                "sender_type": "chat",
                "user_id": 0,
                "sender_chat_id": sender_chat_id,
                "username": username,
                "display_name": title,
                "missing_username_reason": "" if username else "anonymous_or_chat_identity",
            }
        return {
            "sender_type": "unknown",
            "user_id": 0,
            "sender_chat_id": 0,
            "username": "",
            "display_name": "",
            "missing_username_reason": "missing_sender",
        }

    def collect_thread_commenters(self, post_url: str, limit: int = 100) -> dict[str, Any]:
        clean_url = str(post_url or "").strip()
        if not re.match(r"^https://(?:www\.)?(?:t\.me|telegram\.me)/", clean_url, re.IGNORECASE):
            raise TdlibError("帖子链接必须是 https://t.me/... 格式。")
        capped_limit = max(1, min(int(limit), 10000))
        link_info = self.invoke("getMessageLinkInfo", url=clean_url)
        root_message = link_info.get("message") or {}
        root_chat_id = int(root_message.get("chat_id", link_info.get("chat_id", 0)) or 0)
        root_message_id = int(root_message.get("id", 0) or 0)
        if not root_chat_id or not root_message_id:
            raise TdlibError("无法从链接解析帖子；请确认账号能够访问该帖子。")
        can_get_message_thread = root_message.get("can_get_message_thread")
        if can_get_message_thread is None:
            properties = self.invoke(
                "getMessageProperties", chat_id=root_chat_id, message_id=root_message_id
            )
            can_get_message_thread = properties.get("can_get_message_thread", False)
        if not bool(can_get_message_thread):
            raise TdlibError("该帖子没有可读取的评论线程，或当前账号无权访问。")

        thread = self.invoke("getMessageThread", chat_id=root_chat_id, message_id=root_message_id)
        thread_chat_id = int(thread.get("chat_id", 0) or 0)
        thread_message_id = int(thread.get("message_thread_id", 0) or 0)
        if not thread_chat_id or not thread_message_id:
            raise TdlibError("TDLib 未返回评论线程所属的讨论群或线程标识。")

        messages: list[dict[str, Any]] = []
        seen_message_ids: set[int] = set()
        from_message_id = 0
        total_count = 0
        while len(messages) < capped_limit:
            page_limit = min(100, capped_limit - len(messages))
            history = self.invoke(
                "getMessageThreadHistory",
                chat_id=thread_chat_id,
                message_id=thread_message_id,
                from_message_id=from_message_id,
                offset=0,
                limit=page_limit,
            )
            total_count = max(total_count, int(history.get("total_count", 0) or 0))
            batch = [item for item in (history.get("messages") or []) if isinstance(item, dict)]
            new_items = []
            for item in batch:
                message_id = int(item.get("id", 0) or 0)
                if not message_id or message_id in seen_message_ids:
                    continue
                if message_id == thread_message_id and int(item.get("chat_id", 0) or 0) == thread_chat_id:
                    continue
                seen_message_ids.add(message_id)
                new_items.append(item)
            if not new_items:
                break
            messages.extend(new_items)
            next_from = min(int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0))
            if next_from == from_message_id:
                break
            from_message_id = next_from
            if len(batch) < page_limit or (total_count and len(messages) >= total_count):
                break

        sender_cache: dict[tuple[str, int], dict[str, Any]] = {}
        aggregates: dict[tuple[str, int], dict[str, Any]] = {}
        for message in messages[:capped_limit]:
            sender = message.get("sender_id") or {}
            sender_type = str(sender.get("@type", "") or "")
            sender_number = int(sender.get("user_id", sender.get("chat_id", 0)) or 0)
            cache_key = (sender_type, sender_number)
            if cache_key not in sender_cache:
                sender_cache[cache_key] = self._sender_record(sender)
            record = dict(sender_cache[cache_key])
            timestamp = int(message.get("date", 0) or 0)
            date_text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else ""
            aggregate_key = (record["sender_type"], int(record["user_id"] or record["sender_chat_id"] or 0))
            if aggregate_key not in aggregates:
                aggregates[aggregate_key] = {
                    **record,
                    "comment_count": 0,
                    "first_comment_date": date_text,
                    "last_comment_date": date_text,
                }
            aggregate = aggregates[aggregate_key]
            aggregate["comment_count"] += 1
            if date_text:
                if not aggregate["first_comment_date"] or date_text < aggregate["first_comment_date"]:
                    aggregate["first_comment_date"] = date_text
                if not aggregate["last_comment_date"] or date_text > aggregate["last_comment_date"]:
                    aggregate["last_comment_date"] = date_text

        commenters = sorted(
            aggregates.values(),
            key=lambda item: (-int(item["comment_count"]), item["sender_type"], int(item["user_id"] or item["sender_chat_id"])),
        )
        return {
            "thread_chat_id": thread_chat_id,
            "message_thread_id": thread_message_id,
            "post_url": clean_url,
            "root_chat_id": root_chat_id,
            "root_message_id": root_message_id,
            "reported_comment_count": total_count,
            "comments_fetched": min(len(messages), capped_limit),
            "unique_commenters": len(commenters),
            "commenters": commenters,
        }

    def collect_channel_commenters(
        self,
        username: str,
        *,
        post_limit: int = 50,
        comment_limit_per_post: int = 1000,
    ) -> dict[str, Any]:
        state = self.bootstrap()
        if state.get("@type") != "authorizationStateReady":
            raise TdlibError("该账号槽位尚未登录；请先运行 accounts login。")
        clean_username = str(username or "").strip().lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean_username):
            raise TdlibError("频道 username 格式无效。")
        capped_post_limit = max(1, min(int(post_limit), 100))
        capped_comment_limit = max(1, min(int(comment_limit_per_post), 10000))

        chat = self.invoke("searchPublicChat", username=clean_username)
        chat_id = int(chat.get("id", 0) or 0)
        if not chat_id:
            raise TdlibError("无法解析公开频道；请确认 username 正确且账号可访问。")

        messages: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        from_message_id = 0
        for _ in range(20):
            if len(messages) >= capped_post_limit:
                break
            history = self.invoke(
                "getChatHistory",
                chat_id=chat_id,
                from_message_id=from_message_id,
                offset=0,
                limit=min(100, capped_post_limit - len(messages)),
                only_local=False,
            )
            batch = [item for item in (history.get("messages") or []) if isinstance(item, dict)]
            new_items = []
            for item in batch:
                message_id = int(item.get("id", 0) or 0)
                if not message_id or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                new_items.append(item)
            if not new_items:
                break
            messages.extend(new_items)
            next_from = min(int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0))
            if next_from == from_message_id:
                break
            from_message_id = next_from

        candidate_posts: list[tuple[dict[str, Any], int]] = []
        for message in messages:
            reply_info = ((message.get("interaction_info") or {}).get("reply_info") or {})
            reply_count = int(reply_info.get("reply_count", 0) or 0)
            if reply_count > 0:
                candidate_posts.append((message, reply_count))

        aggregates: dict[tuple[str, int], dict[str, Any]] = {}
        thread_reports: list[dict[str, Any]] = []
        total_comments_fetched = 0
        for message, reported_reply_count in candidate_posts:
            message_id = int(message.get("id", 0) or 0)
            properties = self.invoke("getMessageProperties", chat_id=chat_id, message_id=message_id)
            if not bool(properties.get("can_get_message_thread", False)):
                thread_reports.append(
                    {
                        "message_id": message_id,
                        "reported_reply_count": reported_reply_count,
                        "thread_access": "unavailable",
                        "comments_fetched": 0,
                    }
                )
                continue
            link_result = self.invoke(
                "getMessageLink",
                chat_id=chat_id,
                message_id=message_id,
                media_timestamp=0,
                checklist_task_id=0,
                poll_option_id="",
                for_album=False,
                in_message_thread=False,
            )
            post_url = str(link_result.get("link", "") or "")
            if not post_url:
                post_url = f"https://t.me/{clean_username}/{message_id // 1048576}"
            payload = self.collect_thread_commenters(post_url, capped_comment_limit)
            total_comments_fetched += int(payload["comments_fetched"])
            thread_reports.append(
                {
                    "message_id": message_id,
                    "post_url": post_url,
                    "reported_reply_count": reported_reply_count,
                    "thread_access": "readable",
                    "comments_fetched": int(payload["comments_fetched"]),
                    "unique_commenters": int(payload["unique_commenters"]),
                }
            )
            for commenter in payload["commenters"]:
                key = (
                    str(commenter["sender_type"]),
                    int(commenter["user_id"] or commenter["sender_chat_id"] or 0),
                )
                if key not in aggregates:
                    aggregates[key] = {
                        **commenter,
                        "comment_count": 0,
                        "post_urls": set(),
                    }
                aggregate = aggregates[key]
                aggregate["comment_count"] += int(commenter["comment_count"])
                aggregate["post_urls"].add(post_url)
                first_date = str(commenter.get("first_comment_date", "") or "")
                last_date = str(commenter.get("last_comment_date", "") or "")
                if first_date and (
                    not aggregate["first_comment_date"] or first_date < aggregate["first_comment_date"]
                ):
                    aggregate["first_comment_date"] = first_date
                if last_date and (
                    not aggregate["last_comment_date"] or last_date > aggregate["last_comment_date"]
                ):
                    aggregate["last_comment_date"] = last_date

        commenters: list[dict[str, Any]] = []
        for aggregate in aggregates.values():
            commenters.append(
                {
                    **{key: value for key, value in aggregate.items() if not isinstance(value, set)},
                    "post_count": len(aggregate["post_urls"]),
                    "post_urls": ",".join(sorted(aggregate["post_urls"])),
                }
            )
        commenters.sort(
            key=lambda item: (
                -int(item["comment_count"]),
                str(item["sender_type"]),
                int(item["user_id"] or item["sender_chat_id"] or 0),
            )
        )
        return {
            "channel_username": clean_username,
            "chat_id": chat_id,
            "chat_title": str(chat.get("title", "") or ""),
            "posts_scanned": len(messages),
            "posts_with_reported_comments": len(candidate_posts),
            "readable_threads": sum(1 for item in thread_reports if item["thread_access"] == "readable"),
            "comments_fetched": total_comments_fetched,
            "unique_commenters": len(commenters),
            "public_username_count": sum(1 for item in commenters if item.get("username")),
            "unique_user_commenters": sum(
                1 for item in commenters if item.get("sender_type") == "user"
            ),
            "public_user_username_count": sum(
                1
                for item in commenters
                if item.get("sender_type") == "user" and item.get("username")
            ),
            "non_user_sender_count": sum(
                1 for item in commenters if item.get("sender_type") != "user"
            ),
            "thread_reports": thread_reports,
            "commenters": commenters,
        }

    def inspect_public_chat_reactions(
        self,
        username: str,
        *,
        message_limit: int = 30,
        reactor_limit_per_message: int = 1000,
        join_if_needed: bool = False,
    ) -> dict[str, Any]:
        state = self.bootstrap()
        if state.get("@type") != "authorizationStateReady":
            raise TdlibError("该账号槽位尚未登录；请先运行 accounts login。")

        clean_username = str(username or "").strip().lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean_username):
            raise TdlibError("频道 username 格式无效。")
        capped_message_limit = max(1, min(int(message_limit), 100))
        capped_reactor_limit = max(1, min(int(reactor_limit_per_message), 10000))

        chat = self.invoke("searchPublicChat", username=clean_username)
        chat_id = int(chat.get("id", 0) or 0)
        if not chat_id:
            raise TdlibError("无法解析公开频道；请确认 username 正确且账号可访问。")
        chat_type = chat.get("type") or {}
        chat_type_name = str(chat_type.get("@type", "") or "")
        membership_before = "unknown"
        membership_after = "unknown"
        joined_during_probe = False
        if chat_type_name == "chatTypeSupergroup":
            supergroup_id = int(chat_type.get("supergroup_id", 0) or 0)
            if supergroup_id:
                supergroup = self.invoke("getSupergroup", supergroup_id=supergroup_id)
                membership_before = str((supergroup.get("status") or {}).get("@type", "") or "unknown")
                membership_after = membership_before
                if membership_before == "chatMemberStatusBanned":
                    raise TdlibError("当前账号已被该频道封禁，无法读取历史。")
                if membership_before == "chatMemberStatusLeft" and join_if_needed:
                    join_result = self.invoke("joinChat", chat_id=chat_id)
                    join_result_type = str(join_result.get("@type", "") or "")
                    if join_result_type != "chatJoinResultSuccess":
                        raise TdlibError(f"加入频道未立即成功：{join_result_type or 'unknown'}")
                    joined_during_probe = True
                    supergroup = self.invoke("getSupergroup", supergroup_id=supergroup_id)
                    membership_after = str(
                        (supergroup.get("status") or {}).get("@type", "") or "unknown"
                    )

        messages: list[dict[str, Any]] = []
        seen_history_message_ids: set[int] = set()
        from_message_id = 0
        for _ in range(20):
            if len(messages) >= capped_message_limit:
                break
            history = self.invoke(
                "getChatHistory",
                chat_id=chat_id,
                from_message_id=from_message_id,
                offset=0,
                limit=min(100, capped_message_limit - len(messages)),
                only_local=False,
            )
            batch = [item for item in (history.get("messages") or []) if isinstance(item, dict)]
            new_items: list[dict[str, Any]] = []
            for item in batch:
                history_message_id = int(item.get("id", 0) or 0)
                if not history_message_id or history_message_id in seen_history_message_ids:
                    continue
                seen_history_message_ids.add(history_message_id)
                new_items.append(item)
            if not new_items:
                break
            messages.extend(new_items)
            next_from_message_id = min(
                int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0)
            )
            if next_from_message_id == from_message_id:
                break
            from_message_id = next_from_message_id

        sender_cache: dict[tuple[str, int], dict[str, Any]] = {}
        aggregates: dict[tuple[str, int], dict[str, Any]] = {}
        message_reports: list[dict[str, Any]] = []
        full_identity_messages = 0
        recent_only_messages = 0
        hidden_identity_messages = 0
        total_reaction_count = 0
        paid_reaction_count = 0

        def record_sender(
            sender: dict[str, Any],
            *,
            message_id: int,
            reaction_label: str,
            source: str,
            timestamp: int = 0,
        ) -> None:
            sender_type = str(sender.get("@type", "") or "")
            sender_number = int(sender.get("user_id", sender.get("chat_id", 0)) or 0)
            cache_key = (sender_type, sender_number)
            if cache_key not in sender_cache:
                sender_cache[cache_key] = self._sender_record(sender)
            resolved = dict(sender_cache[cache_key])
            aggregate_key = (
                str(resolved["sender_type"]),
                int(resolved["user_id"] or resolved["sender_chat_id"] or 0),
            )
            if aggregate_key not in aggregates:
                aggregates[aggregate_key] = {
                    **resolved,
                    "observed_reaction_count": 0,
                    "message_ids": set(),
                    "reaction_types": set(),
                    "sources": set(),
                    "first_reaction_date": "",
                    "last_reaction_date": "",
                }
            aggregate = aggregates[aggregate_key]
            aggregate["observed_reaction_count"] += 1
            aggregate["message_ids"].add(message_id)
            aggregate["reaction_types"].add(reaction_label)
            aggregate["sources"].add(source)
            if timestamp:
                date_text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                if not aggregate["first_reaction_date"] or date_text < aggregate["first_reaction_date"]:
                    aggregate["first_reaction_date"] = date_text
                if not aggregate["last_reaction_date"] or date_text > aggregate["last_reaction_date"]:
                    aggregate["last_reaction_date"] = date_text

        for message in messages:
            message_id = int(message.get("id", 0) or 0)
            interaction_info = message.get("interaction_info") or {}
            reactions_info = interaction_info.get("reactions") or {}
            reaction_summaries = [
                item for item in (reactions_info.get("reactions") or []) if isinstance(item, dict)
            ]
            if not reaction_summaries:
                continue

            summary_counts: list[dict[str, Any]] = []
            message_reaction_count = 0
            message_paid_count = 0
            for summary in reaction_summaries:
                label = reaction_type_label(summary.get("type"))
                count = int(summary.get("total_count", 0) or 0)
                summary_counts.append({"reaction_type": label, "count": count})
                message_reaction_count += count
                if label == "paid":
                    message_paid_count += count
            total_reaction_count += message_reaction_count
            paid_reaction_count += message_paid_count

            can_get_full = bool(reactions_info.get("can_get_added_reactions", False))
            fetched = 0
            identity_mode = "hidden"
            if can_get_full:
                full_identity_messages += 1
                identity_mode = "full"
                offset = ""
                while fetched < capped_reactor_limit:
                    page = self.invoke(
                        "getMessageAddedReactions",
                        chat_id=chat_id,
                        message_id=message_id,
                        reaction_type=None,
                        offset=offset,
                        limit=min(100, capped_reactor_limit - fetched),
                    )
                    batch = [item for item in (page.get("reactions") or []) if isinstance(item, dict)]
                    for item in batch:
                        record_sender(
                            item.get("sender_id") or {},
                            message_id=message_id,
                            reaction_label=reaction_type_label(item.get("type")),
                            source="full_list",
                            timestamp=int(item.get("date", 0) or 0),
                        )
                    fetched += len(batch)
                    next_offset = str(page.get("next_offset", "") or "")
                    if not batch or not next_offset or next_offset == offset:
                        break
                    offset = next_offset
            else:
                recent_entries = 0
                for summary in reaction_summaries:
                    label = reaction_type_label(summary.get("type"))
                    for sender in summary.get("recent_sender_ids") or []:
                        if not isinstance(sender, dict):
                            continue
                        record_sender(
                            sender,
                            message_id=message_id,
                            reaction_label=label,
                            source="recent_only",
                        )
                        recent_entries += 1
                if recent_entries:
                    recent_only_messages += 1
                    identity_mode = "recent_only"
                    fetched = recent_entries
                else:
                    hidden_identity_messages += 1

            message_reports.append(
                {
                    "message_id": message_id,
                    "date": datetime.fromtimestamp(
                        int(message.get("date", 0) or 0), timezone.utc
                    ).isoformat()
                    if int(message.get("date", 0) or 0)
                    else "",
                    "reaction_count": message_reaction_count,
                    "paid_reaction_count": message_paid_count,
                    "reaction_counts": summary_counts,
                    "can_get_added_reactions": can_get_full,
                    "identity_mode": identity_mode,
                    "identity_records_fetched": fetched,
                }
            )

        reactors: list[dict[str, Any]] = []
        for aggregate in aggregates.values():
            reactors.append(
                {
                    **{key: value for key, value in aggregate.items() if not isinstance(value, set)},
                    "message_count": len(aggregate["message_ids"]),
                    "message_ids": ",".join(str(value) for value in sorted(aggregate["message_ids"])),
                    "reaction_types": ",".join(sorted(aggregate["reaction_types"])),
                    "sources": ",".join(sorted(aggregate["sources"])),
                }
            )
        reactors.sort(
            key=lambda item: (
                -int(item["observed_reaction_count"]),
                str(item["sender_type"]),
                int(item["user_id"] or item["sender_chat_id"] or 0),
            )
        )

        if full_identity_messages:
            overall_identity_access = "full_or_mixed"
        elif recent_only_messages:
            overall_identity_access = "recent_only"
        elif message_reports:
            overall_identity_access = "hidden"
        else:
            overall_identity_access = "no_reactions_observed"

        return {
            "channel_username": clean_username,
            "chat_id": chat_id,
            "chat_title": str(chat.get("title", "") or ""),
            "chat_type": chat_type_name,
            "membership_before": membership_before,
            "membership_after": membership_after,
            "joined_during_probe": joined_during_probe,
            "messages_scanned": len(messages),
            "messages_with_reactions": len(message_reports),
            "reported_reaction_count": total_reaction_count,
            "paid_reaction_count": paid_reaction_count,
            "full_identity_messages": full_identity_messages,
            "recent_only_messages": recent_only_messages,
            "hidden_identity_messages": hidden_identity_messages,
            "identity_access": overall_identity_access,
            "unique_observed_reactors": len(reactors),
            "message_reports": message_reports,
            "reactors": reactors,
        }


@contextmanager
def locked_account_session(
    *,
    account_name: str,
    account_dir: Path,
    credentials: dict[str, Any],
    library_path: str | None = None,
    proxy: dict[str, Any] | None = None,
) -> Iterator[TdlibAccountSession]:
    account_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = account_dir / "runtime.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TdlibError(f"账号槽位 {account_name} 正在被另一个任务使用。") from exc
        try:
            with TdlibAccountSession(
                account_name=account_name,
                account_dir=account_dir,
                api_id=int(credentials["api_id"]),
                api_hash=str(credentials["api_hash"]),
                database_encryption_key=str(credentials["database_encryption_key"]),
                library_path=library_path,
                proxy=proxy,
            ) as session:
                yield session
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def health_check(library_path: str | None = None, timeout_seconds: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    version = "unknown"
    authorization_state = "unknown"
    with TdjsonClient(library_path) as client:
        client.set_log_verbosity(0)
        deadline = time.monotonic() + max(0.5, timeout_seconds)
        while time.monotonic() < deadline:
            update = client.receive(min(0.2, max(0.01, deadline - time.monotonic())))
            if not update:
                continue
            update_type = str(update.get("@type", ""))
            if update_type == "updateOption" and update.get("name") == "version":
                version = str((update.get("value") or {}).get("value") or "unknown")
            if update_type == "updateAuthorizationState":
                authorization_state = str((update.get("authorization_state") or {}).get("@type") or "unknown")
            if version != "unknown" and authorization_state != "unknown":
                break
        resolved = client.library_path
    return {
        "ok": True,
        "tdlib_version": version,
        "authorization_state": authorization_state,
        "library_path": str(resolved),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }
