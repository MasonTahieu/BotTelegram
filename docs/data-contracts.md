# Hợp đồng dữ liệu local (runtime 1D)

File CSV dùng UTF-8 (có thể có BOM), phân cách bằng dấu phẩy. Loader không tự sửa OHLCV sai và không gọi mạng. Cấu hình mẫu là `configs/local-csv.example.toml`; runtime, scanner và CLI backtest hiện chỉ hỗ trợ `timeframe = "1d"`.

## Danh sách mã

```csv
symbol,exchange,name,asset_type,status
FPT,HOSE,Nhãn ví dụ,stock,active
SHS,HNX,Nhãn ví dụ,stock,active
ACV,UPCOM,Nhãn ví dụ,stock,active
```

`universe_path` trỏ tới file danh sách mã. Mã gồm 2–10 ký tự in hoa/chữ số và không trùng; `exchange` là HOSE, HNX hoặc UPCOM; `status` là active, halted hoặc delisted. Chỉ `asset_type=stock` thuộc universe quét, mã không active được liệt kê nhưng scanner bỏ qua. `symbols=[]` trong TOML chọn toàn bộ mã phù hợp; có thể đặt nhóm nhỏ để chạy thử. File ví dụ không phải universe toàn thị trường.

## Giá OHLCV ngày

```csv
symbol,timeframe,closed_at,open,high,low,close,volume,is_closed,observed_at
FPT,1d,2026-09-18T14:45:00+07:00,100,102,99,101,100000,true,2026-09-18T14:45:00+07:00
```

Dòng ví dụ chỉ minh họa schema, **không đủ** để tính chỉ báo. Strategy mặc định cần ít nhất **51 nến ngày** để so sánh EMA50 ở hai phiên cuối; `prepare-history.cmd` yêu cầu tối thiểu **52 nến** cho cache chuẩn bị. Nên có chuỗi dài hơn để EMA ổn định.

- Cột bắt buộc của CSV giá: `symbol`, `timeframe`, `closed_at`, `open`, `high`, `low`, `close`, `volume`, `is_closed`. `observed_at` có thể bỏ trống cho nến đã đóng; loader dùng `closed_at` khi bỏ trống.
- `timeframe` chỉ nhận `1d`. `closed_at` là mốc **đóng nến** có múi giờ: HOSE 14:45, HNX/UPCOM 15:00 giờ Việt Nam vào ngày giao dịch (trừ ngày nghỉ đã khai báo). Adapter nguồn thật chuẩn hóa thời gian trước khi ghi vào hệ thống.
- `is_closed` chỉ dùng `true` hoặc `false`. Nến chưa đóng phải có `observed_at` hợp lệ trước mốc đóng, cùng ngày giao dịch và không trước 09:00. Scanner chỉ dùng nến đã đóng, đã được quan sát ở hoặc trước thời điểm quét.
- Giá phải hữu hạn, dương; High/Low bao quanh Open/Close. Khối lượng là số nguyên không âm. Chuỗi theo từng mã phải tăng dần, không trùng phiên, đúng lịch sàn và đúng mã/khung. Không tự tạo nến giả cho ngày thiếu hoặc mã không giao dịch.
- `CsvDataProvider` đọc lại file khi thời điểm sửa đổi thay đổi. Nên thay file hoàn chỉnh để tránh bot đọc khi file đang ghi dở.

`/chart` trong Telegram dùng cache daily chuẩn hóa của chế độ Vietcap/KBS, không vẽ từ CSV hay gọi Internet. Backtest từ CSV vẫn dùng validation nến ngày đã đóng như scanner.

## Báo cáo tài chính CSV

```csv
symbol,period,published_on,eps,pe,pb,roe,debt_to_equity,revenue_growth,profit_growth
FPT,2026-Q2,2026-08-01,1000,10,1.5,0.15,0.5,0.10,0.10
```

Số liệu ví dụ là giả định. Cột bắt buộc: `symbol`, `period`, `published_on`. Các chỉ tiêu còn lại có thể trống; giá trị có mặt phải là số hữu hạn. `roe=0.15` tương ứng 15%. Loader giữ bản có ngày công bố mới nhất của từng mã; dữ liệu tài chính không nằm trong công thức EMA/RSI. Khi người dùng bật filter, dữ liệu thiếu, chưa công bố hoặc quá cũ không vượt qua điều kiện; filter chỉ gate BUY, không chặn SELL.

Vietcap financials thêm `observed_on` (ngày tải) và `ratio_basis` (cơ sở tỷ số). Các tỷ số tải ở hiện tại không phải dữ liệu point-in-time cho backtest quá khứ. Xem [tài liệu Vietcap](vietcap.md).

## Demo và dữ liệu cũ

Nguồn demo dùng giá, khối lượng và financials giả lập để kiểm tra hành vi. Bản hiện tại chỉ tạo/quét nến `1d`. File nến 5m hoặc report minute có từ phiên bản cũ là **dữ liệu lịch sử**, không được CSV loader, scanner hay strategy runtime hiện tại sử dụng.

