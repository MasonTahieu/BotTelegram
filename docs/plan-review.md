# Đối chiếu đề Bài tập 3 và bản GPT_WORK_FIX_PLAN_DU_AN_3

**Ghi chú:** Đây là bản đối chiếu kế hoạch trước khi hoàn thiện bản nộp; không dùng làm mô tả runtime hoặc danh sách lệnh hiện hành. Xem [báo cáo dự án hiện tại](project-report.md).

Bài tập yêu cầu bot Telegram hỗ trợ tra cứu/cảnh báo Mua-Bán cổ phiếu Việt Nam, có dữ liệu giá/khối lượng lịch sử và hiện tại, chỉ tiêu tài chính cơ bản, ít nhất một chiến lược và tài liệu/demo. Backtest là phần mở rộng. Đề **không** quy định phải quét đủ 1.523 mã, giữ 5m, tạo cảnh báo sớm, tự sửa cache liên tục, hoặc kết hợp quote khác nguồn vào indicator.

## Giữ vì trực tiếp giúp độ đúng và khả năng demo

- Chiến lược EMA20/EMA50 + RSI14, 1D, 900 giây cấu hình; không sửa ngưỡng/quy tắc.
- KBS historical với Vietcap full-series fallback, checkpoint và khóa collector. Mỗi lần đánh giá dùng một chuỗi 1D đã đóng từ một nguồn.
- Quote Vietcap theo lô để hiển thị riêng. Dữ liệu tài chính và bộ lọc BUY theo yêu cầu; SELL không bị chặn.
- Subscription, SQLite outbox, `/signals today`, Telegram long poll/backoff, fail-closed.
- Migration chọn 5m cũ sang 1d mà không xóa subscription hay dữ liệu 5m lịch sử.

## Gọt bỏ hoặc sửa trong bản đề xuất

- Bỏ preview/early và 1m→5m trong runtime. Nến chưa đóng không tính tín hiệu.
- Không mang logic compatibility/corporate-action giữa KBS history và Vietcap quote vào hot path: hai loại dữ liệu không còn merge. Điều chỉnh doanh nghiệp vẫn là giới hạn phải đối chiếu khi dùng dữ liệu thật/backtest.
- Không bắt buộc 90%/100% universe READY hay xem số mã thiếu của một snapshot đang bootstrap là bug code. Mã mới niêm yết, ngừng giao dịch, thiếu lịch sử hoặc bị giới hạn nguồn sẽ fail-closed.
- Không biến giá quote READY thành historical READY; hai phần có trạng thái độc lập.
- Không cố xây hệ thống tự repair hàng trăm mã và tải lại mọi financial report trong mỗi lượt scan. Historical chuẩn bị bằng job riêng; khi cần phiên mới, chạy lại job rồi quét.
- Không bỏ `/filter`/`/screen` vì đề yêu cầu dữ liệu tài chính phục vụ lọc cơ bản và code hiện có đã hỗ trợ. Bộ lọc chỉ tải khi được dùng.
- Không xóa cột `early_alerts`, dữ liệu 5m hoặc database cũ chỉ để “dọn sạch”; giữ migration an toàn nhưng bỏ đường chạy cũ.
- Không cam kết exactly-once tuyệt đối cho Telegram: mạng có thể đứt sau khi server nhận tin nhưng trước phản hồi.
- Sửa ví dụ CLI sai `vnstock_sync --timeframe` (parser không có option đó), tách `prepare-history.cmd` khỏi `prepare-vietcap.cmd`, và cập nhật README theo hành vi thực.

## Giới hạn còn lại

Cache historical không tự đổi sau phiên nếu chưa chạy lại `prepare-history.cmd`. Bot sẽ báo STALE và không tạo tín hiệu mới cho mã đó. Số lượng mã READY thực tế phụ thuộc nguồn và cần đo trên lần chuẩn bị dữ liệu thật; test tự động dùng fixture/fake, không chứng minh độ phủ production. Token trong `.secrets` phải được loại khỏi file nén nộp bài.
