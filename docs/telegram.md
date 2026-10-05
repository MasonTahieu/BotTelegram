# Tạo và chạy bot Telegram

Bot dùng Telegram Bot API để nhận lệnh qua long polling và gửi cảnh báo; không cần tài khoản chứng khoán Vietcap. Telegram chỉ chạy với cấu hình live/CSV, không chạy ở chế độ demo để tránh gửi dữ liệu giả lập.

## Thiết lập lần đầu

1. Tìm **@BotFather** trên Telegram, gửi `/newbot`, đặt tên và username kết thúc bằng `bot`.
2. Trong thư mục dự án, chạy `start-telegram-setup.cmd`. Hộp thoại kiểm tra token và lưu vào `.secrets/telegram-token.txt`. Có thể dùng `python -B -m fintech_bot setup-telegram --console` nếu không có giao diện. Không đưa token hoặc thư mục `.secrets` vào bản nộp. Biến môi trường `TELEGRAM_BOT_TOKEN` được ưu tiên hơn file token.
3. Chạy `prepare-history.cmd` để chuẩn bị/catch-up nến ngày đã đóng. Chạy lại sau phiên giao dịch khi cần phiên mới. Nếu dùng chỉ tiêu tài chính, chạy thêm `prepare-vietcap.cmd`.
4. Chạy `start-telegram.cmd`, mở link bot được in ra và gửi `/start` trong **chat riêng**. Máy cần bật, có mạng và tiến trình cần tiếp tục chạy để trả lời.

Không chạy bộ chuẩn bị dữ liệu song song với bot; các tác vụ dùng chung khóa ở thư mục dữ liệu. Bot không tự ghép quote trong phiên vào historical hay tự tải lại toàn bộ historical mỗi 15 phút.

## Lệnh hiện tại

| Lệnh | Tác dụng |
|---|---|
| `/start`, `/help` | Hướng dẫn. |
| `/status` | Trạng thái historical, live quote, financials và lần quét. |
| `/universe [SÀN]` | Danh sách mã, tối đa 50 dòng; SÀN có thể là HOSE, HNX hoặc UPCOM. |
| `/signals [trang]` | Tín hiệu BUY/SELL đang có trong lần quét gần nhất. |
| `/stock <MÃ>` | Giá/quote hiện tại và trạng thái tín hiệu của mã. |
| `/chart <MÃ>` | Gửi PNG gồm khoảng 100 nến ngày Close, EMA20, EMA50, RSI14 và tín hiệu gần nhất nếu có. Chỉ tạo ảnh theo yêu cầu, đọc cache local và xóa file tạm sau khi gửi. |
| `/financials <MÃ>` | Chỉ tiêu tài chính, kỳ/ngày công bố và Amihud ILLIQ 20 phiên nếu có dữ liệu. |
| `/subscribe <MÃ>`, `/unsubscribe <MÃ>` | Thêm/bỏ mã theo dõi. |
| `/subscriptions` | Danh sách mã đang theo dõi. |
| `/alerts watchlist`, `/alerts all`, `/alerts off` | Chọn phạm vi cảnh báo tự động. |
| `/filter`, `/filter <tên> <số>`, `/filter clear` | Xem, đặt hoặc xóa bộ lọc của chat. Ví dụ: `/filter roe_min 15`, `/filter pe_max 20`, `/filter illiq_max 1e-10`. Đây là ví dụ cú pháp, không phải ngưỡng khuyến nghị. |
| `/screen [trang]` | Liệt kê mã đạt các điều kiện lọc đang bật. |
| `/scan` | Xếp lượt quét lại historical cache 1D đã đóng và yêu cầu cập nhật live quote; xem tiến độ bằng `/status`. |
| `/timeframe 1d` | Khung duy nhất được hỗ trợ. |

Mỗi chat lưu riêng subscriptions, alert mode và bộ lọc trong SQLite. Bộ lọc chỉ gate BUY trong `/signals` và alert; SELL vẫn hiển thị/gửi. Mã thiếu dữ liệu để tính điều kiện được xem là không đạt. `/stock` và `/status` có thể hiển thị quote Vietcap; giá đó **không** được tính vào EMA/RSI. `/chart` báo bằng text nếu mã không thuộc universe hoặc cache historical chưa READY. Bot bỏ qua group/channel; `/step` và `/exit` không dùng trên Telegram.

## Vận hành và giới hạn

`configs/vietcap.toml` đặt `timeframe = "1d"` và `scan_interval_seconds = 900`. Trong Telegram live, 900 giây là chu kỳ cập nhật batch quote; historical catch-up là bước riêng qua `prepare-history.cmd`. `/scan` đánh giá cache hiện có, không hứa tải nến historical mới. Bản live không dùng nến 5m, không có `/early`. Một lượt quét nhiều mã được chia lô nhỏ giữa các lần đọc lệnh để bot vẫn phản hồi.

Nếu token sai/thu hồi, cấu hình lại qua BotFather và trình cài đặt. Lỗi 409 thường là một tiến trình `getUpdates` khác đang dùng bot hoặc webhook còn hoạt động; dừng tiến trình kia hay dùng bot riêng. Với 429, bot chờ theo thời gian máy chủ yêu cầu. Bot lưu offset và tiến độ trả lời trong SQLite; kết nối mất ngay sau khi Telegram nhận tin nhưng trước khi bot nhận xác nhận vẫn có khả năng lặp tin.

Xem thêm [kiến trúc](architecture.md), [failover](vnstock-failover.md) và [Telegram Bot API](https://core.telegram.org/bots/api).




