# Historical Vnstock/KBS và failover Vietcap

## Nguồn dữ liệu thực tế

| Nhu cầu | Luồng hiện tại |
|---|---|
| Universe HOSE/HNX/UPCOM | Vnstock 4.0.8 với `source="kbs"`; có thể dùng danh sách Vietcap đã lưu để dựng nguồn dự phòng. |
| Historical 1D | KBS là nguồn ưu tiên khi chuẩn bị; Vietcap historical là nguồn dự phòng. Chuỗi của từng mã lấy từ **một** nguồn, không ghép candle KBS với Vietcap. |
| Live quote cho `/stock`, `/status` | Vietcap batch quote qua `fintech_bot/services/live_data.py`; tách hẳn historical. |
| Financials và `/financials` | KBS chưa cung cấp ngày công bố đáng tin cậy theo hợp đồng đã kiểm tra; `get_financials_result` báo thiếu khả năng để `DataProviderManager` dùng Vietcap IQ. |
| Indicator và backtest | Chỉ dùng historical daily đã đóng; quote trong phiên không được đưa vào EMA20/EMA50/RSI14. |

Cấu hình live tại `configs/vietcap.toml` chọn `primary_provider = "vnstock"`, `backup_provider = "vietcap"`, `timeframe = "1d"` và `enable_failover = true`. Adapter Vnstock dùng `source="kbs"`; không tự chuyển sang VCI.

## Chuẩn bị và catch-up

`prepare-history.cmd` gọi `fintech_bot/data/vnstock_sync.py`, có checkpoint/resume ở `data/vnstock/bootstrap-progress.json`. Mỗi mã được đối chiếu với **phiên giao dịch đã đóng gần nhất** theo sàn, nên trong phiên không đòi nến của ngày đang hình thành. Cache đã đủ được `REUSED`, không gọi historical API. Cache thiếu vài phiên được tải phần gần nhất, merge/deduplicate theo ngày; chỉ cache thiếu/hỏng hoặc dưới mức tối thiểu 52 nến mới bootstrap chuỗi dài.

Khi cache thiếu đúng một phiên, job kiểm tra Vietcap batch quote của phiên đó. Quote xác nhận **không giao dịch** thì kết quả `NO_NEW_CANDLE` và bỏ historical request. Nếu quote cho thấy có giao dịch hoặc không đủ bằng chứng, job vẫn thử incremental historical. Thiếu nhiều hơn một phiên luôn phải catch-up, dù quote mới nhất không có giao dịch. Quote chỉ là điều kiện giảm request; nó không trở thành nến historical.

Bootstrap nguồn chính thử KBS trước rồi Vietcap khi KBS không đạt. Khi cache đã chọn một nguồn, incremental cập nhật theo nguồn đó; không trộn delta của nguồn khác vào chuỗi. Kết quả có `REUSED`, `NO_NEW_CANDLE`, `UPDATED`, `BOOTSTRAPPED`, `ERROR` và `READY`. `prepare-vnstock.cmd` chỉ là tên tương thích cũ trỏ sang `prepare-history.cmd`.

## Runtime và kiểm tra dữ liệu

`fintech_bot/data/provider_manager.py` đọc cache chuẩn `data/daily/` trước, rồi cache KBS và Vietcap khi cần. Runtime scan **không gọi historical API**. `fintech_bot/data/daily_cache.py` chỉ chấp nhận `data_source` là `VNSTOCK_KBS` hoặc `VIETCAP_DIRECT`; cache đời cũ có provenance trộn nguồn bị từ chối để chuẩn bị lại. Mỗi chuỗi được kiểm tra schema, mã/khung, thứ tự ngày, OHLCV, lịch phiên, tối thiểu số nến cần và độ mới. Nếu dữ liệu không READY, `Scanner` bỏ mã đó và hủy thông báo đang chờ của mã/khung; không phát BUY/SELL mới từ bản cũ.

KBS cache bị thiếu phiên đóng mới nhất không được coi là chuỗi READY. Vietcap có thể được chấp nhận là historical đã được refresh nhưng nến cuối lùi phiên do mã không giao dịch; trường hợp này được gắn `no_recent_trade` và Scanner **không** phát tín hiệu mới. Tên nguồn được lưu cùng signal để truy vết, còn khóa chống trùng không phụ thuộc nguồn.

`scan_interval_seconds = 900` trong live là chu kỳ cập nhật quote/lịch kiểm tra, không phải cam kết historical 1D có nến mới sau 900 giây. Sau phiên cần dừng bot, chạy `prepare-history.cmd` rồi khởi động lại khi muốn quét trên nến đóng mới. Không chạy collector cùng lúc với bot do khóa dữ liệu chung. Các lỗi mạng, rate limit hoặc cache không hợp lệ được ghi trạng thái; hệ thống không tự lấp nến hay lấy quote trong phiên thay historical.

Xem [kiến trúc](architecture.md), [hướng dẫn Telegram](telegram.md) và [hợp đồng dữ liệu](data-contracts.md).


