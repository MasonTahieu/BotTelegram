# Vai trò Vietcap trong phiên bản hiện tại

Vietcap có **ba nhánh độc lập**: historical 1D dự phòng cho KBS, batch live quote để hiển thị, và dữ liệu tài chính Vietcap IQ. Bot không đặt lệnh hay đăng nhập tài khoản chứng khoán. Các endpoint công khai đã dùng trong project không đòi cookie/API key ở lần kiểm tra; điều đó không bảo đảm quyền truy cập hoặc phân phối lâu dài.

## Historical 1D dự phòng

`prepare-history.cmd` ưu tiên Vnstock 4.0.8/KBS cho chuỗi nến ngày đã đóng. Nếu bootstrap KBS không đạt validation, job thử Vietcap historical và lưu **cả chuỗi của một mã** với `data_source = VIETCAP_DIRECT` vào `data/daily/`. Khi đã có cache đủ nến, job chỉ catch-up phần thiếu theo nguồn của cache; không trộn candle KBS và Vietcap để tính chỉ báo. Scanner đọc cache local qua `DataProviderManager`; scan không gọi API historical.

Vietcap batch quote có một vai trò phụ trong `prepare-history`: nếu cache chỉ thiếu đúng một phiên, quote của phiên đó xác nhận mã không giao dịch thì job báo `NO_NEW_CANDLE` và không gọi historical API. Nếu thiếu nhiều phiên hoặc quote không đủ bằng chứng, job vẫn catch-up. Quote **không** được ghi thành nến historical.

Để chuẩn bị riêng nhóm nhỏ:

```powershell
.\prepare-history.cmd --symbols FPT SHS ACV
```

Cần dừng bot live/Telegram trước khi chạy job chuẩn bị vì cùng dùng khóa dữ liệu. Sau phiên mới, chạy lại `prepare-history.cmd` để có nến đã đóng mới nhất.

## Batch live quote và financials

`start-live.cmd` và `start-telegram.cmd` chạy `LiveData` (`fintech_bot/services/live_data.py`), cập nhật batch quote Vietcap phục vụ `/stock` và `/status`. Cấu hình `scan_interval_seconds = 900` là chu kỳ cập nhật quote/lịch kiểm tra, không phải lịch tải historical hay cam kết trễ 900 giây cho từng mã. Quote trong phiên có thể chưa đóng và không được ghép vào EMA20/EMA50/RSI14. `/scan` yêu cầu làm mới bảng giá và quét lại historical cache đang có.

`prepare-vietcap.cmd` chuẩn bị riêng `ratios` và `income` để dùng cho `/financials` và các filter. Lệnh tương đương:

```powershell
.\_python.cmd -B -m fintech_bot.data.vietcap_sync --config configs/vietcap.toml --components ratios income --resume --max-age 604800 --timeout 30 --workers 1 --interval 2
```

Bộ tải tài chính dùng một tác vụ, chờ tối đa 30 giây và giãn yêu cầu 2 giây. Sau 10 lỗi mạng liên tiếp, bộ tải nghỉ 30 giây rồi tiếp tục phần chưa thử, tối đa hai lần nghỉ; lỗi riêng của một mã không dừng cả danh sách. Nguồn từ chối truy cập vẫn khiến lượt tải dừng. Báo cáo phân biệt lỗi và phần chưa thử; chạy lại với `--resume` để thử lại phần lỗi. Các thay đổi này không thay luồng `prepare-history.cmd`.

`/financials <MÃ>` hiển thị kỳ, ngày công bố, chỉ tiêu hiện có và Amihud ILLIQ 20 phiên tính từ historical cache nếu đủ dữ liệu. Financial filter là tùy chọn theo chat; dữ liệu thiếu hoặc quá cũ không qua filter. Filter chỉ gate BUY, không chặn SELL. Các tỷ số tài chính tải ở hiện tại không được đưa ngược vào backtest quá khứ.

## Dữ liệu và giới hạn

Vietcap universe lấy từ bảng giá, lọc cổ phiếu HOSE/HNX/UPCOM. Historical và quote được chuẩn hóa theo giá đồng/cổ phiếu, khối lượng cổ phiếu và mốc đóng của sàn. Cache không tự điền phiên thiếu bằng nến giả. `data/vietcap/` giữ dữ liệu nguồn và trạng thái tải; `data/daily/` giữ chuỗi historical chuẩn hóa được scanner dùng; SQLite lưu candles/tín hiệu đã quét và tùy chọn/cảnh báo.

Runtime chỉ hỗ trợ `1d`. File minute và các báo cáo thử nghiệm 5m cũ còn trên đĩa là **kết quả lịch sử của phiên bản cũ**, không phải chức năng đang chạy. `ONE_MINUTE`, nến 5m, `/early` và ghép quote vào historical không nằm trong đường chạy hiện tại. Các report coverage cũ trong `reports/` phản ánh thời điểm ghi trên file, không chứng minh readiness ở phiên mới nhất; kiểm tra lại bằng `/status` và cache hiện tại.

Xem [kiến trúc](architecture.md), [failover](vnstock-failover.md) và [hướng dẫn Telegram](telegram.md).

