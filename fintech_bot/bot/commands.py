"""Transport-independent bot commands and per-user preferences."""

import logging

from fintech_bot.bot.formatting import format_signal, source_label
from fintech_bot.data.readiness import public_data_error
from fintech_bot.data.base import FetchStatus, ProviderResult
from fintech_bot.domain import SignalSide
from fintech_bot.market import ReplayClock
from fintech_bot.services.financial_filter import validate_rule

logger = logging.getLogger(__name__)

HELP = """FINTECH BOT · TRỢ GIÚP
Bot phân tích nến ngày đã đóng; không hỗ trợ đặt lệnh.

TRA CỨU
/stock <MÃ> · tra cứu một cổ phiếu
/chart <MÃ> · biểu đồ kỹ thuật 100 nến đã đóng
/financials <MÃ> · thông tin tài chính của cổ phiếu
/signals [trang] · tín hiệu Mua/Bán theo trang

THEO DÕI
/subscribe <MÃ> · theo dõi một cổ phiếu
/unsubscribe <MÃ> · hủy theo dõi một cổ phiếu
/subscriptions · danh sách đang theo dõi
/alerts watchlist|all|off · phạm vi thông báo tự động

SÀNG LỌC
/filter · xem bộ lọc tài chính của bạn
/filter roe_min 15 · ví dụ ROE tối thiểu 15%
/filter pe_max 20 · ví dụ P/E dương, không vượt 20
/filter illiq_max <giá trị> · Amihud ILLIQ 20 phiên
/filter clear · tắt tất cả điều kiện lọc
/screen [trang] · các mã đạt bộ lọc

DỮ LIỆU / HỆ THỐNG
/status · trạng thái dữ liệu và lịch chạy
/universe [HOSE|HNX|UPCOM] · mã trong nguồn hiện tại
/scan · yêu cầu cập nhật và quét nến ngày
/start hoặc /help · xem hướng dẫn

Trong demo, toàn bộ giá đều giả lập. Quote trong phiên không dùng để tính EMA/RSI."""


class CommandRouter:
    def __init__(self, scanner, repository, alerts):
        self.scanner, self.repository, self.alerts = scanner, repository, alerts
        self.live_data = None
        self.scan_requested = False

    def consume_scan_request(self):
        requested, self.scan_requested = self.scan_requested, False
        return requested

    def handle(self, text, chat_id="local-demo"):
        try:
            self.alerts.register(str(chat_id))
            return self._handle(text, str(chat_id))
        except Exception:
            logger.exception("Command processing failed")
            return "Không xử lý được lệnh lúc này. Chi tiết đã được ghi vào nhật ký."

    def _scan(self):
        report = self.scanner.run()
        alerts = self.alerts.dispatch(report.signals)
        succeeded = len({signal.symbol for signal in report.signals})
        result = (f"Đã quét nến ngày: {succeeded}/{len(self.scanner.symbols)} mã hợp lệ; "
                  f"{len(report.signals)}/{len(self.scanner.symbols)} kết quả 1d. "
                  f"Thông báo mới: {alerts.sent}; đang chờ: {alerts.pending}.")
        errors = [f"{key}: {value}" for key, value in report.errors.items()]
        skipped = [f"{key}: {value}" for key, value in report.skipped.items()]
        return "\n".join([result, *errors[:10], *skipped[:10], *alerts.errors[:10]])

    def _refresh(self, symbol=None, timeframe=None):
        if self.live_data is not None and symbol is None:
            # A full market refresh is asynchronous. Command handling must not
            # wait behind a whole-universe scan.
            self.live_data.request_refresh()
            return None
        report = self.scanner.refresh_if_needed(symbol, timeframe)
        if report is not None:
            self.alerts.dispatch(report.signals)

    def _handle(self, text, chat_id):
        parts = text.strip().split()
        if not parts:
            return "Nhập /help để xem hướng dẫn."
        command = parts[0].lower()
        mode, timeframe = self.repository.preference(chat_id)
        no_args = {"/start", "/help", "/status", "/subscriptions", "/scan", "/step"}
        symbol_commands = {"/stock", "/financials", "/subscribe", "/unsubscribe"}
        choices = {"/alerts": {"watchlist", "all", "off"}}
        if command in no_args and len(parts) != 1:
            return f"Lệnh {command} không nhận tham số."
        if command in {"/start", "/help"}:
            return HELP
        if command == "/status":
            clock = self.scanner.clock
            label = "ĐỒNG HỒ GIẢ LẬP" if isinstance(clock, ReplayClock) else "GIỜ THỰC"
            provider_status = getattr(self.scanner.provider, "status_snapshot", lambda: None)()
            scan = (provider_status or {}).get("scan") or {}
            live = (self.live_data.status_snapshot() if self.live_data
                    and hasattr(self.live_data, "status_snapshot") else {})
            historical = scan.get("historical", {})
            quote = live.get("live_quote", {})
            scanner = scan.get("scanner", {})
            financials = live.get("financials", {})
            def rows(values, names):
                return "\n".join(f"{name}: {values.get(name, 0)}" for name in names)
            requested = scan.get("requested", len(self.scanner.symbols))
            cache_state = "ready" if requested and historical.get("READY", 0) == requested else "incomplete"
            scan_stamp = scan.get("finished_at", "chưa có lượt quét hoàn tất")
            return (f"TRẠNG THÁI HỆ THỐNG\n{label}: {clock.now().isoformat()}\n"
                    f"Nguồn dữ liệu: {source_label(self.scanner.provider.source, is_demo=self.scanner.provider.is_demo)}\n"
                    f"Lần quét gần nhất: {scan_stamp}\n"
                    f"Universe: {len(self.scanner.symbols)}\n"
                    f"Khung: 1d\n"
                    f"Lịch quét: {self.scanner.settings.scan_interval_seconds}s\n"
                    f"Freshness: {self.scanner.settings.stale_after_seconds}s\n\n"
                    "━━ HISTORICAL DAILY ━━\n"
                    + rows(historical, ("READY", "MISSING", "STALE", "INVALID", "ERROR")) + "\n\n"
                    "━━ LIVE QUOTE ━━\n"
                    + rows(quote, ("READY", "NO_TRADE", "MISSING", "STALE", "INVALID", "ERROR")) + "\n\n"
                    "━━ SCANNER ━━\n"
                    + rows(scanner, ("ELIGIBLE", "SKIPPED", "BUY", "SELL", "NONE")) + "\n"
                    "SKIPPED reasons:\n"
                    + rows(scanner.get("SKIPPED_REASONS", {}), ("no_recent_trade", "historical_missing",
                          "historical_invalid", "insufficient_history", "other")) + "\n\n"
                    "━━ FINANCIALS ━━\n"
                    + rows(financials, ("READY", "MISSING", "STALE", "INVALID", "ERROR")) + "\n\n"
                    f"Thời gian scan: {scan.get('duration_seconds', 0):.1f}s\n"
                    f"Vietcap requests lượt live gần nhất: {live.get('provider_requests', 0)}\n"
                    f"Tin đang chờ: {self.repository.pending_count()}\n"
                    f"Priority queue: {live.get('priority_queue', 0)}\n\n"
                    f"Historical cache: {cache_state}\n"
                    "Live quote: Vietcap batch (chỉ hiển thị, không ghép vào EMA/RSI)\n"
                    "MISSING/STALE/INVALID/ERROR: signal suppressed")
        if command == "/filter":
            rules = self.repository.financial_filter(chat_id)
            if len(parts) == 2 and parts[1].lower() == "clear":
                self.repository.set_financial_filter(chat_id, {})
                return "Đã tắt bộ lọc tài chính."
            if len(parts) == 3:
                try:
                    rules[parts[1].lower()] = validate_rule(parts[1].lower(), float(parts[2]))
                except ValueError as error:
                    return ("Test nhập sai định dạng chữ thay vì số."
                            if str(error).startswith("could not convert string to float:") else str(error))
                self.repository.set_financial_filter(chat_id, rules)
            elif len(parts) != 1:
                return "Cú pháp: /filter [roe_min|pe_max|pb_max|debt_max|eps_min|illiq_max SỐ] hoặc /filter clear."
            return ("Bộ lọc của bạn: " + (", ".join(f"{key}={value:g}" for key, value in rules.items()) or "chưa bật")
                    + ".\nÁp dụng cho /signals và thông báo tự động của bạn. ROE nhập theo %, nợ/vốn theo lần. "
                      "Thiếu dữ liệu sẽ không vượt qua bộ lọc. /stock vẫn cho xem tín hiệu kèm kết quả lọc.")
        if command == "/screen":
            if not self.repository.financial_filter(chat_id):
                return "Hãy chọn ít nhất một điều kiện bằng /filter trước. Các ngưỡng do bạn quyết định."
            if len(parts) > 2 or len(parts) == 2 and (not parts[1].isdigit() or int(parts[1]) < 1):
                return "Cú pháp: /screen [số trang từ 1]"
            passed = [symbol for symbol in self.scanner.symbols if self.alerts.financial_filter.check(chat_id, symbol)[0]]
            page = int(parts[1]) if len(parts) == 2 else 1
            pages = max(1, (len(passed) + 29) // 30)
            return (f"SÀNG LỌC  •  Trang {page}/{pages}\n"
                    f"{len(passed)}/{len(self.scanner.symbols)} mã đạt bộ lọc.\n"
                    "Mã thiếu dữ liệu hoặc không đạt đều bị loại. Đây chưa phải tín hiệu Mua.\n\n"
                    + (", ".join(passed[(page-1)*30:page*30]) or "Không có mã ở trang này."))
        if command in choices:
            if len(parts) != 2 or parts[1].lower() not in choices[command]:
                return f"Cú pháp: {command} " + " | ".join(sorted(choices[command]))
            value = parts[1].lower()
            mode = value
            self.repository.set_preference(chat_id, mode, timeframe)
            return f"Đã lưu lựa chọn của bạn: thông báo {mode}, khung {timeframe}."
        if command == "/timeframe":
            if len(parts) != 2:
                return "Cú pháp: /timeframe 1d"
            value = parts[1].lower()
            if value == "5m":
                self.repository.set_preference(chat_id, mode, "1d")
                return "Khung 5m đã ngừng hỗ trợ. Bot đã tự chuyển bạn sang 1d; các subscription vẫn được giữ nguyên."
            if value != "1d":
                return "Bot chỉ hỗ trợ /timeframe 1d."
            self.repository.set_preference(chat_id, mode, "1d")
            return f"Đã lưu lựa chọn của bạn: thông báo {mode}, khung 1d."
        if command == "/scan":
            if self.live_data:
                self.live_data.request_refresh()
                self.scan_requested = True
                return ("Đã xếp một lượt quét historical 1d đã đóng và làm mới bảng giá. "
                        "Bảng giá không được ghép vào EMA/RSI; dùng /status để xem tiến độ.")
            return self._scan()
        if command == "/step":
            return "Lệnh /step đã ngừng hỗ trợ vì bot chỉ còn dùng khung nến ngày 1d."
        if command == "/universe":
            if len(parts) > 2 or (len(parts) == 2 and parts[1].upper() not in self.scanner.settings.exchanges):
                return "Cú pháp: /universe [HOSE|HNX|UPCOM]"
            items = [item for item in self.scanner.instruments.values()
                     if len(parts) == 1 or item.exchange == parts[1].upper()]
            rows = [f"{item.symbol} | {item.exchange} | {item.status}" for item in items[:50]]
            return f"{len(items)} mã trong danh sách local (hiện tối đa 50):\n" + "\n".join(rows)
        if command == "/signals":
            today = len(parts) > 1 and parts[1].lower() == "today"
            page_parts = parts[2:] if today else parts[1:]
            if len(page_parts) > 1 or (page_parts and (not page_parts[0].isdigit() or int(page_parts[0]) < 1)):
                return "Cú pháp: /signals [today] [số trang từ 1]"
            self._refresh()
            page = int(page_parts[0]) if page_parts else 1
            report = self.scanner.latest_report
            if today:
                items = self.repository.signals_on(self.scanner.clock.now().date(),
                    self.scanner.strategy.settings.strategy_id, None, timeframe,
                    is_demo=self.scanner.provider.is_demo)
                items = [item for item in items if item.symbol in self.scanner.instruments]
            else:
                items = [item for item in report.signals if item.timeframe == timeframe and item.side != SignalSide.NONE]
            items = [item for item in items if self.alerts.financially_eligible(chat_id, item)]
            pages = max(1, (len(items) + 4) // 5)
            if page > pages:
                return f"Có {pages} trang tín hiệu."
            results = [format_signal(item) for item in items[(page-1)*5:page*5]]
            errors = [f"{key}: {value}" for key, value in report.errors.items() if key.endswith('/' + timeframe)]
            title = f"TÍN HIỆU  •  Khung {timeframe}  •  Trang {page}/{pages}\n{len(items)} tín hiệu Mua/Bán  |  {len(errors)} lỗi dữ liệu."
            if today:
                title = f"Lịch sử ngày {self.scanner.clock.now():%d/%m/%Y} (chỉ ghi nhận khi bot đã quét)\n" + title
            return "\n\n".join([title, *(results or ["Không có tín hiệu Mua/Bán mới."]), *errors[:5]])
        if command == "/subscriptions":
            symbols = self.repository.subscriptions(chat_id)
            return "Đang theo dõi: " + ", ".join(symbols) if symbols else "Bạn chưa theo dõi mã nào."
        if command not in symbol_commands:
            return "Lệnh chưa được hỗ trợ. Nhập /help để xem danh sách."
        if len(parts) != 2:
            return f"Cú pháp: {command} MÃ. Ví dụ: {command} {self.scanner.symbols[0]}"
        symbol = parts[1].upper()
        if command == "/unsubscribe":
            removed = self.repository.unsubscribe(chat_id, symbol)
            return f"Đã hủy theo dõi {symbol}." if removed else f"Bạn chưa theo dõi {symbol}."
        if symbol not in self.scanner.symbols:
            return f"Mã {symbol} chưa có trong demo hoặc danh sách local. Dùng /universe để xem."
        if command == "/subscribe":
            added = self.repository.subscribe(chat_id, symbol)
            if self.live_data:
                self.live_data.request_symbol(symbol, "1d", priority=1)
            return f"Đã theo dõi {symbol}." if added else f"Bạn đã theo dõi {symbol}."
        if command == "/financials":
            managed = hasattr(self.scanner.provider, "status_snapshot")
            if self.live_data and not managed:
                health = self.live_data.ensure_components(symbol, ("ratios", "income"))
                if not all(item.ready for item in health.values()):
                    return self.live_data.readiness_message(symbol, health)
            try:
                fetch = getattr(self.scanner.provider, "get_financials_result", None)
                result = fetch(symbol) if fetch else None
                if result is not None and not isinstance(result, ProviderResult):
                    raise ValueError("Nguồn tài chính sai data contract.")
                if result is not None:
                    if result.meta.status is not FetchStatus.READY or result.value is None:
                        if self.live_data:
                            self.live_data.request_components(symbol, ("ratios", "income"))
                        raise ValueError(result.error or result.meta.fallback_reason or "Dữ liệu chưa READY.")
                    snapshot, financial_source = result.value, result.meta.source
                else:
                    snapshot = self.scanner.provider.get_financials(symbol)
                    financial_source = self.scanner.provider.source
                if snapshot is None:
                    return f"{symbol}: chưa có dữ liệu tài chính."
                if snapshot.published_on > self.scanner.clock.now().date():
                    return f"{symbol}: báo cáo chưa đến ngày công bố."
                if snapshot.observed_on is not None and snapshot.observed_on > self.scanner.clock.now().date():
                    return f"{symbol}: bản dữ liệu được thu thập sau ngày đang kiểm tra."
                self.repository.save_financials(financial_source, snapshot)
                illiq = self.alerts.financial_filter.illiq(symbol)
                illiq_text = f"{illiq:.3e}" if illiq is not None else "chưa có"
                def number(value, percent=False):
                    return "chưa có" if value is None else (f"{value * 100:.2f}%" if percent else f"{value:,.2f}")
                return (f"{symbol}  |  DỮ LIỆU TÀI CHÍNH\n"
                        f"Kỳ báo cáo  {snapshot.period}  ·  Công bố {snapshot.published_on:%d/%m/%Y}\n\n"
                        "ĐỊNH GIÁ & HIỆU QUẢ\n"
                        f"EPS cơ bản của kỳ (đồng): {number(snapshot.eps)}\n"
                        f"P/E: {number(snapshot.pe)}  |  P/B: {number(snapshot.pb)}\n"
                        f"ROE: {number(snapshot.roe, True)}  |  Nợ/vốn chủ sở hữu: {number(snapshot.debt_to_equity)}\n"
                        f"\nTHANH KHOẢN\nAmihud ILLIQ (20 phiên): {illiq_text}\n\n"
                        "NGUỒN & GHI CHÚ\n"
                        f"Nguồn: {source_label(financial_source, is_demo=self.scanner.provider.is_demo)}\n"
                        + (f"Thu thập {snapshot.observed_on:%d/%m/%Y}; cơ sở tỷ số: {snapshot.ratio_basis}. "
                           "EPS của kỳ và tỷ số TTM/năm có kỳ tính khác nhau. Không dùng bản tải hiện tại để giả định đã biết trong quá khứ."
                           if snapshot.observed_on else "Dữ liệu giả lập." if self.scanner.provider.is_demo else "Dữ liệu từ file local."))
            except (OSError, ValueError, KeyError, TypeError) as error:
                return f"{symbol}: chưa có dữ liệu tài chính hợp lệ — {public_data_error(error)}"
        managed = hasattr(self.scanner.provider, "status_snapshot")
        if self.live_data and not managed:
            self.live_data.request_symbol(symbol, timeframe)
            report = self.scanner.run([symbol], (timeframe,))
            self.alerts.dispatch(report.signals)
        else:
            self._refresh(symbol, timeframe)
        report = self.scanner.latest_report
        key = f"{symbol}/{timeframe}"
        if key in report.errors:
            return f"{symbol}: Lần quét mới nhất bị lỗi — {report.errors[key]}"
        if symbol in report.skipped:
            return f"{symbol}: {report.skipped[symbol]}"
        signal = next((item for item in report.signals if item.symbol == symbol and item.timeframe == timeframe), None)
        result = format_signal(signal) if signal else "Chưa có kết quả cho khung này."
        hint = getattr(self.scanner.provider, "status_hint", lambda _: "")(symbol)
        if hint and (signal is None or hint not in signal.reason):
            result += "\n" + hint
        if self.repository.financial_filter(chat_id):
            result += "\n\nBộ lọc tài chính: " + self.alerts.financial_filter.check(chat_id, symbol)[1]
        if self.live_data:
            quote = self.live_data.quote_summary(symbol)
            if quote:
                result += "\n\nBẢNG GIÁ HIỆN TẠI\n" + quote
        return result
