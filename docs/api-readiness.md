# Mức sẵn sàng dữ liệu API

Tài liệu này mô tả **code hiện tại**. Các kiểm tra 5m/ONE_MINUTE trong report cũ là thử nghiệm của phiên bản trước; runtime và CLI hiện chỉ nhận `1d`.

## Luồng dữ liệu đang dùng

| Thành phần | Source và hành vi |
|---|---|
| Universe | Vnstock 4.0.8/KBS với danh sách Vietcap đã lưu làm dữ liệu bổ trợ khi dựng ứng dụng. |
| Historical | `prepare-history.cmd` ưu tiên KBS, dùng Vietcap historical dự phòng khi cần, ghi nến 1D đã đóng vào cache chuẩn hóa `data/daily/`. Chuỗi mỗi mã chỉ có một nguồn. |
| Live quote | Vietcap batch quote qua `LiveData`, hiển thị ở `/stock`, `/status`; không vào EMA/RSI. |
| Financials | KBS chưa có ngày công bố tin cậy theo hợp đồng hiện tại nên `DataProviderManager` dùng Vietcap IQ. Dữ liệu phục vụ `/financials` và filter BUY tùy chọn. |
| Scanner | Chỉ đọc historical cache local, validate trước khi tính EMA20/EMA50/RSI14. Nếu thiếu/cũ/sai thì bỏ mã và không phát tín hiệu mới. |

`scan_interval_seconds = 900` là chu kỳ cấu hình cho quote/lịch kiểm tra; không đồng nghĩa mỗi mã có nến historical mới sau 900 giây. Catch-up historical là job riêng sau phiên. Không chạy job chuẩn bị song song với bot. `/scan` quét lại cache hiện có và yêu cầu refresh quote.

## Data contract và validation

`fintech_bot/data/base.py` định nghĩa `MarketDataProvider`, `CandleBatch`, `ProviderResult` và metadata trạng thái. `DataProviderManager` chọn cache chuẩn, cache KBS hoặc Vietcap theo từng mã; runtime không tải historical qua mạng. `LocalDailyCache` chỉ chấp nhận nến `1d` đã đóng với `data_source` hợp lệ. Nguồn mixed provenance cũ bị từ chối.

`Scanner` kiểm tra mã, khung, OHLCV, thứ tự phiên, số nến tối thiểu, mốc đóng theo sàn và thời điểm quan sát. Nến chưa đóng, nến tương lai và quote hiện tại không vào indicator. Lượt quét hợp lệ lưu candles và signal vào SQLite; nếu lịch sử thay đổi, tín hiệu được tính lại. Dữ liệu lỗi vô hiệu thông báo còn chờ cho mã/khung đó.

`prepare-history` có checkpoint/resume, cập nhật incremental khi cache còn hợp lệ; với khoảng thiếu đúng một phiên, quote Vietcap xác nhận không giao dịch có thể giảm một historical request (`NO_NEW_CANDLE`). Nếu quote không chắc hoặc thiếu nhiều phiên, job vẫn thử catch-up. Không tự điền nến giả cho phiên không giao dịch.

## Giới hạn cần trình bày khi nộp

- Chưa bảo đảm coverage 100% universe hay độ trễ nguồn trong phiên; `/status` cho thấy readiness thực tế của lượt quét.
- Lịch nghỉ bất thường phải được thêm vào cấu hình sau khi xác minh; trạng thái mã bị hạn chế/tạm ngừng không phải lúc nào cũng có trong nguồn.
- Giá điều chỉnh, mốc ATC/PLO và công bố tài chính có thể khác giữa nguồn; validation loại chuỗi không hợp lệ thay vì tự suy diễn.
- Các report coverage, benchmark và thử nghiệm minute cũ chỉ có giá trị tại thời điểm tạo. Không dùng chúng làm số liệu current nếu chưa chạy lại.

Xem [kiến trúc](architecture.md), [failover](vnstock-failover.md), [hợp đồng dữ liệu](data-contracts.md) và [hướng dẫn Telegram](telegram.md).

