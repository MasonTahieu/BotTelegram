# Bàn giao dự án Fintech Telegram Bot

Tài liệu này là danh mục bàn giao theo **source hiện tại**. Đề gốc: [Baitap3.pdf](../Baitap3.pdf). Hướng dẫn cài và chạy đầy đủ nằm ở [README](../README.md); phần giải thích và đối chiếu đề nằm ở [báo cáo dự án](project-report.md). Không dùng các số liệu độ phủ hoặc kết quả thử 5m từ bản cũ để mô tả runtime này.

## Thành phần bàn giao

| Hạng mục | File/bằng chứng | Trạng thái |
|---|---|---|
| Mã nguồn Python và cấu hình | [fintech_bot](../fintech_bot/), [configs](../configs/), [pyproject.toml](../pyproject.toml) | Có trong source; yêu cầu Python 3.11+ |
| Hướng dẫn cài, chuẩn bị dữ liệu, chạy và kiểm thử | [README](../README.md), [hướng dẫn Telegram](telegram.md) | Có; người nhận cần cài dependency và chuẩn bị cache trên máy của mình |
| Kiến trúc và dữ liệu | [sơ đồ kiến trúc](architecture.md), [tài liệu failover](vnstock-failover.md) | Historical 1d đóng: Vnstock/KBS chính, Vietcap dự phòng; không trộn hai nguồn trong một chuỗi chỉ báo |
| Logic chiến lược | [chiến lược EMA20/EMA50 + RSI14](strategy.md), [source chỉ báo](../fintech_bot/strategies/indicators.py), [source tín hiệu](../fintech_bot/strategies/ema_rsi.py) | BUY/SELL/NONE trên nến ngày đã đóng |
| Telegram Bot | [commands.py](../fintech_bot/bot/commands.py), [telegram.py](../fintech_bot/bot/telegram.py) | Có adapter, lệnh và cảnh báo; cần token BotFather và tiến trình đang chạy để dùng bot thật |
| Lưu trữ và cảnh báo | [sqlite.py](../fintech_bot/storage/sqlite.py), [alerts.py](../fintech_bot/services/alerts.py) | SQLite lưu dữ liệu và lựa chọn từng chat; alert có outbox |
| Backtest | [backtest.py](../fintech_bot/services/backtest.py), [reports](../reports/) và mục backtest trong [báo cáo](project-report.md) | Mô phỏng trên historical 1d; xem ngày, mã, nguồn và chỉ tiêu cụ thể trong báo cáo mới nhất |
| Kiểm thử tự động | [tests](../tests/) | Chạy theo README để đối chiếu bản nhận được; không lấy số test của lần bàn giao cũ làm trạng thái hiện tại |

## Phạm vi hoạt động của bản này

Bot chỉ tính EMA20, EMA50 và RSI14 từ historical **1d đã đóng**. Cache được chuẩn bị bằng [prepare-history.cmd](../prepare-history.cmd), có cập nhật incremental, checkpoint và fallback Vietcap khi KBS không cho chuỗi hợp lệ. Vietcap batch quote là nhánh hiển thị /stock, /status; quote trong phiên không thêm vào historical. Dữ liệu tài chính phục vụ /financials và bộ lọc tùy chọn; các điều kiện lọc chỉ gate BUY, không chặn SELL. Lệnh /chart tạo PNG từ cache local khi người dùng yêu cầu.

Các lệnh chính gồm /status, /universe, /signals, /stock, /chart, /subscribe, /unsubscribe, /subscriptions, /alerts, /financials, /filter, /screen và /scan. Nội dung và cú pháp đang hiển thị trong /help của [commands.py](../fintech_bot/bot/commands.py). Runtime không tính 5m và không có tín hiệu sớm từ quote. Chu kỳ cấu hình là 900 giây; trong chế độ live, /scan yêu cầu quét historical đã chuẩn bị. Phần mềm không gửi lệnh mua bán tới công ty chứng khoán.

## Những việc cần làm trên máy nghiệm thu

1. Cài Python và package theo [README](../README.md); chạy thử local bằng [start-local.cmd](../start-local.cmd).
2. Chuẩn bị historical bằng [prepare-history.cmd](../prepare-history.cmd). Nếu muốn dùng dữ liệu tài chính, chạy thêm [prepare-vietcap.cmd](../prepare-vietcap.cmd). Kiểm tra /status trước khi diễn giải số mã READY hoặc tín hiệu.
3. Tạo bot riêng trong BotFather, lưu token bằng [start-telegram-setup.cmd](../start-telegram-setup.cmd), rồi chạy [start-telegram.cmd](../start-telegram.cmd). **Không đưa token, thư mục .secrets, file .env, cơ sở dữ liệu cá nhân hoặc log vào gói chia sẻ.**
4. Kiểm tra lệnh và cảnh báo với tài khoản Telegram thật trước khi cung cấp liên kết bot cho giảng viên. Source không chứa token hoặc liên kết bot đã được nghiệm thu; bot cần máy chạy và kết nối mạng.

Dữ liệu nguồn có thể thiếu, cũ hoặc bị giới hạn; universe không đồng nghĩa mọi mã đều có historical READY. Backtest chỉ là mô phỏng: giả định khớp giá mở cửa phiên sau, phí và trượt giá theo [backtest.py](../fintech_bot/services/backtest.py), không chứng minh hiệu quả đầu tư. Các giới hạn và kết quả thực tế được ghi trong [báo cáo dự án](project-report.md).
