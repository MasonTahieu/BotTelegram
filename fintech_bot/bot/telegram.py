"""Small Telegram Bot API adapter with redacted errors and durable update progress."""

import getpass
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile

from fintech_bot.bot.charting import render_chart_image
from fintech_bot.config import PROJECT_ROOT
from fintech_bot.data.daily_cache import LocalDailyCache
from fintech_bot.data.validation import validate_candles, validate_market_times
from fintech_bot.strategies.indicators import ema, rsi
from fintech_bot.storage.lock import ProcessLock

TOKEN_FILE = PROJECT_ROOT / ".secrets" / "telegram-token.txt"
logger = logging.getLogger(__name__)


class TelegramError(ValueError):
    def __init__(self, message, *, retry_after=0, permanent=False, status=None,
                 error_code=None, description="", exception_type="", method="", elapsed=0.0):
        super().__init__(message)
        self.retry_after, self.permanent = retry_after, permanent
        self.status, self.error_code = status, error_code
        self.description, self.exception_type = description, exception_type
        self.method, self.elapsed = method, elapsed


class TelegramClient:
    def __init__(self, token, opener=None):
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Token Telegram sai định dạng. Nhập token do BotFather cung cấp trên máy, không gửi vào cuộc trò chuyện.")
        self._token, self.opener = token, opener or urllib.request.urlopen
        self.bot_id = token.split(":", 1)[0]
        self.retry_until = 0.0

    def call(self, method, payload=None, timeout=15, photo=None):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage", "sendPhoto"}:
            raise ValueError("Phương thức Telegram không được hỗ trợ.")
        remaining = self.retry_until - time.monotonic()
        if remaining > 0:
            raise TelegramError("Telegram yêu cầu chờ trước khi thử lại.", retry_after=int(remaining) + 1)
        if photo is None:
            data = json.dumps(payload or {}).encode()
            content_type = "application/json"
        else:
            boundary = "FintechBot" + secrets.token_hex(12)
            chat_id = str(payload["chat_id"]).encode("ascii")
            data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n".encode()
                    + chat_id + f"\r\n--{boundary}\r\n".encode()
                    + b'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                    + b"Content-Type: image/png\r\n\r\n" + photo
                    + f"\r\n--{boundary}--\r\n".encode())
            content_type = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/{method}",
            data=data, headers={"Content-Type": content_type})
        http_status = None
        started = time.monotonic()
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise TelegramError("Phản hồi Telegram quá lớn.")
            result = json.loads(raw)
        except urllib.error.HTTPError as error:
            http_status = error.code
            try:
                result = json.loads(error.read(100_000))
            except (ValueError, OSError):
                result = {"ok": False, "error_code": error.code}
        except urllib.error.URLError as error:
            # Never include str(error): urllib exceptions may contain the secret URL.
            kind = type(getattr(error, "reason", error)).__name__
            raise TelegramError("Không kết nối được Telegram.", exception_type=kind,
                                method=method, elapsed=time.monotonic() - started) from None
        except (OSError, TimeoutError) as error:
            raise TelegramError("Không kết nối được Telegram.", exception_type=type(error).__name__,
                                method=method, elapsed=time.monotonic() - started) from None
        except ValueError as error:
            raise TelegramError("Phản hồi Telegram không phải JSON hợp lệ.",
                                exception_type=type(error).__name__, method=method,
                                elapsed=time.monotonic() - started) from None
        if not isinstance(result, dict):
            raise TelegramError("Telegram trả sai cấu trúc phản hồi.", method=method,
                                elapsed=time.monotonic() - started)
        if result.get("ok") is not True:
            code = result.get("error_code", "không rõ")
            description = result.get("description", "")
            description = description[:500] if isinstance(description, str) else ""
            retry = result.get("parameters", {}).get("retry_after", 0)
            retry = retry if isinstance(retry, (float, int)) and 0 <= retry <= 86400 else 0
            self.retry_until = time.monotonic() + retry
            raise TelegramError(f"Telegram trả lỗi {code}. " + {
                401: "Token không hợp lệ hoặc đã bị thu hồi.",
                403: "Người dùng đã chặn bot hoặc bot không có quyền gửi.",
                409: "HTTP 409 Conflict: có thể có tiến trình getUpdates khác hoặc còn webhook.",
                429: "Đã chạm giới hạn gửi; sẽ đợi trước khi thử lại.",
            }.get(code, "Chưa hoàn tất yêu cầu."), retry_after=retry,
                permanent=code in {400, 401, 403, 409}, status=http_status or code,
                error_code=code, description=description, method=method,
                elapsed=time.monotonic() - started)
        return result.get("result")

    def verify(self):
        profile = self.call("getMe")
        if not isinstance(profile, dict) or not profile.get("is_bot") or not profile.get("username"):
            raise TelegramError("Token không trả về thông tin bot hợp lệ.")
        webhook = self.call("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise TelegramError("Bot đang có webhook. Hãy dùng bot riêng hoặc gỡ webhook hiện tại trước khi chạy local.", permanent=True)
        return profile

    def updates(self, offset, poll_timeout=25):
        poll_timeout = max(1, min(30, int(poll_timeout)))
        result = self.call("getUpdates", {"offset": offset, "limit": 20,
                                          "timeout": poll_timeout,
                                          "allowed_updates": ["message"]},
                           timeout=poll_timeout + 12)
        if not isinstance(result, list):
            raise TelegramError("Danh sách tin nhắn Telegram không hợp lệ.")
        return result

    def send(self, chat_id, text):
        # Alerts are small. Longer command responses are split by TelegramRunner,
        # whose durable cursor prevents resending already acknowledged chunks.
        for part in split_message(text):
            self.send_part(chat_id, part)

    def send_part(self, chat_id, text):
        if not re.fullmatch(r"-?\d+", str(chat_id)):
            raise TelegramError("Người nhận không phải chat Telegram hợp lệ.", permanent=True)
        self.call("sendMessage", {"chat_id": chat_id, "text": text,
                                 "link_preview_options": {"is_disabled": True}})


    def send_photo(self, chat_id, path):
        if not re.fullmatch(r"-?\d+", str(chat_id)):
            raise TelegramError("Người nhận không phải chat Telegram hợp lệ.", permanent=True)
        self.call("sendPhoto", {"chat_id": chat_id}, timeout=30, photo=Path(path).read_bytes())


def render_chart(app, symbol, path):
    """Render one requested PNG from the normalized local daily cache."""
    if symbol not in app.scanner.instruments:
        raise ValueError(f"{symbol}: m\u00e3 kh\u00f4ng c\u00f3 trong universe.")
    settings = app.scanner.settings
    if settings.vnstock_path is None:
        raise ValueError(f"{symbol}: ch\u01b0a c\u00f3 historical daily cache local.")
    try:
        candles, fetched, source = LocalDailyCache(
            settings.vnstock_path.parent / "daily").read(symbol)
        exchange = app.scanner.instruments[symbol].exchange
        validate_candles(symbol, candles, settings.strategy.required_bars)
        validate_market_times(candles, app.scanner.calendar, exchange)
        expected = app.scanner.calendar.latest_end(app.clock.now(), exchange, "1d")
        if fetched < expected or candles[-1].timestamp > expected:
            raise ValueError("Historical daily cache ch\u01b0a READY.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{symbol}: historical daily ch\u01b0a READY.") from error

    closes = [candle.close for candle in candles]
    fast = ema(closes, settings.strategy.ema_fast)
    slow = ema(closes, settings.strategy.ema_slow)
    strength = rsi(closes, settings.strategy.rsi_period)
    visible = candles[-100:]
    offset = len(candles) - len(visible)
    signal = app.repository.latest_action_signal(
        symbol, settings.strategy.strategy_id, visible[0].session,
        visible[-1].session, is_demo=app.scanner.provider.is_demo)
    render_chart_image(path, symbol, exchange, source, visible, closes, fast, slow,
                       strength, offset, signal, settings.strategy.ema_fast,
                       settings.strategy.ema_slow, settings.strategy.rsi_period,
                       is_demo=app.scanner.provider.is_demo)


def split_message(text, limit=3500):
    """Use UTF-16 code units so emoji cannot overflow Telegram's length limit."""
    result, part, length = [], [], 0
    for char in str(text):
        units = 2 if ord(char) > 0xFFFF else 1
        if length + units > limit:
            result.append("".join(part))
            part, length = [], 0
        part.append(char)
        length += units
    if part:
        result.append("".join(part))
    return result or ["Không có nội dung."]


def load_token(path=TOKEN_FILE):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and Path(path).exists():
        token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("Chưa có token Telegram. Chạy start-telegram-setup.cmd và làm theo hướng dẫn.")
    return token


def store_token(token, path=TOKEN_FILE):
    """Replace the saved token atomically; a failed write preserves the old file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=".telegram-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(token.strip() + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def setup_telegram(path=TOKEN_FILE, *, console=False):
    if os.name == "nt" and not console:
        from fintech_bot.bot.telegram_setup import setup_window
        return setup_window(path)
    print("1. Mở Telegram, tìm @BotFather (tài khoản chính thức có dấu xác minh).")
    print("2. Gửi /newbot, đặt tên và username kết thúc bằng bot.")
    print("3. Dán token vào dòng bên dưới rồi nhấn Enter. Ký tự và cả dấu * đều không hiện khi nhập.")
    token = getpass.getpass("Token Telegram: ").strip()
    profile = TelegramClient(token).verify()  # Read-only account validation.
    store_token(token, path)
    print(f"Đã lưu token trên máy. Bot: https://t.me/{profile['username']}")
    print("Chạy start-telegram.cmd, rồi mở bot và gửi /start. Không chia sẻ thư mục .secrets.")
    return 0


class TelegramRunner:
    def __init__(self, app, client, username):
        self.app, self.client, self.username = app, client, username
        self.key = "telegram:" + client.bot_id
        self.state = app.repository.get_state(self.key, {"offset": 0, "pending": None})
        self.last_command = {}

    def _save(self):
        self.app.repository.set_state(self.key, self.state)

    def _reply(self):
        pending = self.state["pending"]
        if pending is None:
            return
        if pending.get("chart"):
            with NamedTemporaryFile(suffix=".png", delete=False) as stream:
                path = Path(stream.name)
            try:
                try:
                    render_chart(self.app, pending["chart"], path)
                except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
                    pending["parts"] = split_message(str(error))
                    pending.pop("chart")
                    self._save()
                else:
                    try:
                        self.client.send_photo(pending["chat"], path)
                    except TelegramError as error:
                        if not error.permanent or "401" in str(error) or "409" in str(error):
                            raise
            finally:
                path.unlink(missing_ok=True)
            if pending.get("chart"):
                self.state["offset"] = pending["update_id"] + 1
                self.state["pending"] = None
                self._save()
                return
        while pending["next_part"] < len(pending["parts"]):
            try:
                self.client.send_part(pending["chat"], pending["parts"][pending["next_part"]])
            except TelegramError as error:
                if not error.permanent:
                    raise
                if "401" in str(error) or "409" in str(error):
                    raise
                # A blocked chat/malformed recipient must not block other users.
                break
            pending["next_part"] += 1
            self._save()
        self.state["offset"] = pending["update_id"] + 1
        self.state["pending"] = None
        self._save()

    def once(self, poll_timeout=25):
        self._reply()
        if isinstance(self.client, TelegramClient):
            updates = self.client.updates(self.state["offset"], poll_timeout=poll_timeout)
        else:
            updates = self.client.updates(self.state["offset"])
        for update in updates:
            update_id = update.get("update_id")
            if type(update_id) is not int or update_id < self.state["offset"]:
                continue
            message = update.get("message", {})
            chat = message.get("chat", {})
            text = message.get("text", "")
            # Personal preferences have one clear owner in private chat. Ignore
            # groups/channels and service updates; don't disclose paths to groups.
            if chat.get("type") != "private" or not isinstance(text, str) or not text.startswith("/"):
                self.state["offset"] = update_id + 1
                self._save()
                continue
            command, *tail = text.split(maxsplit=1)
            if "@" in command:
                command, recipient = command.split("@", 1)
                if recipient.lower() != self.username.lower():
                    self.state["offset"] = update_id + 1
                    self._save()
                    continue
            chat_id = str(chat["id"])
            # Bound expensive scans/screens per chat. Preferences remain responsive.
            expensive = command in {"/stock", "/scan", "/signals", "/screen", "/financials", "/chart"}
            previous = self.last_command.get(chat_id, -30)
            chart_symbol = None
            if expensive and time.monotonic() - previous < 5:
                reply = "Hãy đợi 5 giây giữa các lần tra cứu. Bot vẫn đang cập nhật dữ liệu."
            elif command in {"/exit", "/step"}:
                reply = "Lệnh này chỉ dùng trong cửa sổ mô phỏng local."
            else:
                if expensive:
                    self.last_command[chat_id] = time.monotonic()
                if command == "/chart":
                    args = tail[0].split() if tail else []
                    if len(args) != 1:
                        reply = "Cú pháp: /chart <MÃ>"
                    elif args[0].upper() not in self.app.scanner.symbols:
                        reply = f"Mã {args[0].upper()} chưa có trong universe."
                    else:
                        chart_symbol, reply = args[0].upper(), None
                else:
                    reply = self.app.commands.handle(" ".join([command, *tail]), chat_id)
            self.state["pending"] = {"update_id": update_id, "chat": chat_id,
                                      "parts": split_message(reply) if reply is not None else [],
                                      "next_part": 0}
            if chart_symbol:
                self.state["pending"]["chart"] = chart_symbol
            self._save()
            self._reply()


class IncrementalScanner:
    """Scan one small daily batch between Telegram polling turns."""

    def __init__(self, app, chunk_size=25):
        self.app = app
        self.chunk_size = max(1, int(chunk_size))
        self.pending = []
        self._rescan_requested = False

    @property
    def active(self):
        return bool(self.pending)

    def schedule(self, symbols):
        selected = list(dict.fromkeys(symbols))
        if self.pending:
            self._rescan_requested = True
            return
        self.pending = selected
        begin = getattr(self.app.scanner, "begin_cycle", None)
        if begin is not None:
            begin(selected)

    def step(self):
        if not self.pending:
            return False
        chunk = self.pending[:self.chunk_size]
        del self.pending[:self.chunk_size]
        report = self.app.scanner.run(chunk)
        self.app.alerts.dispatch(report.signals)
        if not self.pending:
            finish = getattr(self.app.scanner, "finish_cycle", None)
            if finish is not None:
                finish()
            if self._rescan_requested:
                self._rescan_requested = False
                self.pending = list(self.app.scanner.symbols)
                begin = getattr(self.app.scanner, "begin_cycle", None)
                if begin is not None:
                    begin(self.pending)
        return True


def run_telegram(app, client, profile, live_data=None):
    runner = TelegramRunner(app, client, profile["username"])
    scanner = IncrementalScanner(app)
    with ProcessLock(PROJECT_ROOT / "data" / f"telegram-{client.bot_id}.lock"):
        print(f"Bot @{profile['username']}: long polling đã bắt đầu — Ctrl+C để dừng.")
        retry_at = 0.0
        transient_failures = 0
        next_scan = None
        while True:
            # Poll first so commands never wait behind a whole-market scan.
            if time.monotonic() >= retry_at:
                try:
                    runner.once(poll_timeout=1 if scanner.active else 25)
                    transient_failures = 0
                except TelegramError as error:
                    transient_failures += 1
                    delay = max(error.retry_after, min(30, 5 * (2 ** (transient_failures - 1))))
                    logger.error("Telegram request failed method=%s status=%s error_code=%s "
                                 "description=%r exception_type=%s elapsed=%.2fs retry_delay=%s",
                                 error.method, error.status, error.error_code, error.description,
                                 error.exception_type, error.elapsed, delay)
                    print(str(error))
                    if error.permanent or transient_failures >= 5:
                        raise
                    retry_at = time.monotonic() + delay
            if live_data:
                finished = live_data.tick()
                if finished is not None:
                    print(live_data.describe())
                if app.commands.consume_scan_request():
                    scanner.schedule(app.scanner.symbols)
            else:
                now = app.clock.now()
                if app.scanner.calendar.is_scan_time(now) and (next_scan is None or now.timestamp() >= next_scan):
                    scanner.schedule(app.scanner.symbols)
                    next_scan = now.timestamp() + app.scanner.settings.scan_interval_seconds
            if not scanner.step():
                app.alerts.flush()
            if time.monotonic() < retry_at:
                time.sleep(min(1.0, retry_at - time.monotonic()))
