# Báo cáo dự án — Fintech Bot (bản nộp hiện tại)

## 1. Mục tiêu và phạm vi

Theo [đề Baitap3.pdf](../Baitap3.pdf), project xây dựng bot hỗ trợ theo dõi cổ phiếu Việt Nam, thu thập dữ liệu giá và tài chính, tạo tín hiệu kỹ thuật, tương tác Telegram và có báo cáo thử nghiệm. Source hiện tại hỗ trợ HOSE, HNX, UPCOM trên **một khung nến ngày 1d**, với chiến lược EMA20/EMA50 + RSI14. Bot chỉ cung cấp tín hiệu tham khảo, không kết nối đặt lệnh hay đưa ra khuyến nghị đầu tư cá nhân.

## 2. Kiến trúc và nguồn dữ liệu

Luồng chính được minh họa trong [sơ đồ kiến trúc](architecture.md). `prepare-history.cmd` chuẩn bị nến ngày đã đóng: Vnstock 4.0.8/KBS là historical chính; nếu chuỗi không đạt kiểm tra, Vietcap cung cấp **một chuỗi dự phòng đầy đủ**. Không trộn nến của hai nguồn trong chuỗi EMA/RSI cho cùng một mã. `fintech_bot/data/vnstock_sync.py` catch-up theo phần thiếu, có checkpoint/resume; `daily_cache.py` lưu cache chuẩn hóa; `provider_manager.py` đọc nguồn sẵn có khi chạy bot.

Vietcap batch quote là nhánh độc lập cho `/stock`, `/status` và kiểm tra giao dịch khi chuẩn bị history. Quote trong phiên không ghép vào historical. Dữ liệu tài chính Vietcap IQ được lưu riêng và phục vụ `/financials` cùng bộ lọc của người dùng. Runtime live/Telegram cập nhật quote theo chu kỳ **900 giây**; nến historical mới cần chạy bước chuẩn bị riêng sau phiên. `/scan` quét historical cache hiện có.

## 3. Data pipeline và kiểm tra chất lượng

Nến đầu vào được chuẩn hóa thành OHLCV 1d và chỉ dùng khi phiên đã đóng. Validation kiểm tra cấu trúc, giá trị OHLCV, thứ tự/thời gian phiên, số nến tối thiểu và độ mới. Chuỗi không READY bị scanner bỏ qua, tránh phát signal từ dữ liệu thiếu hoặc hỏng. Khi cache đã đến phiên đóng gần nhất, `prepare-history` REUSED và không gọi historical API; thiếu đúng một phiên có thể dùng Vietcap batch quote để xác nhận NO_NEW_CANDLE; thiếu nhiều phiên vẫn incremental catch-up. Cache chưa có, dưới 52 nến hoặc invalid thì bootstrap. Kết quả tải và nguồn thực được giữ theo mã.

`fintech_bot/services/scanner.py` đọc cache và đánh giá chiến lược; `fintech_bot/storage/sqlite.py` lưu candles, signals, preferences, subscriptions và notification outbox. `fintech_bot/services/alerts.py` chọn người nhận theo chế độ watchlist/all/off, chống lặp và thử lại tin chưa gửi. Chức năng Telegram trong `fintech_bot/bot/commands.py` và `telegram.py`: `/signals`, `/stock`, `/chart`, `/subscribe`, `/alerts`, `/financials`, `/filter`, `/screen`, `/scan`, `/status` và các lệnh hỗ trợ. `/chart <MÃ>` chỉ tạo PNG theo yêu cầu từ cache local khoảng 100 nến, có EMA20/EMA50 và RSI14; ảnh tạm được xóa sau khi gửi.

## 4. Chiến lược và bộ lọc

Chiến lược trong `fintech_bot/strategies/indicators.py` và `ema_rsi.py` dùng EMA20, EMA50, Wilder RSI14 trên giá đóng của nến ngày đã hoàn tất. BUY khi EMA20 cắt lên EMA50 từ dưới hoặc bằng và RSI14 hiện tại > 50. SELL khi EMA20 cắt xuống EMA50, **hoặc** RSI14 rời vùng >70 xuống ≤70. SELL được ưu tiên nếu điều kiện BUY và SELL cùng xảy ra; còn lại NONE. Công thức, xử lý biên và ví dụ chi tiết: [strategy.md](strategy.md).

Financial filter là tùy chọn theo người dùng (`roe_min`, `pe_max`, `illiq_max` và các chỉ tiêu đang có). Amihud ILLIQ dùng 20 phiên gần nhất: trung bình `|ln(Close_d / Close_(d-1))| / (Close_d × Volume_d)` khi không có giá trị giao dịch riêng. Bộ lọc có thể gate BUY kỹ thuật; **không chặn SELL**. Lọc tài chính và signal kỹ thuật là hai bước khác nhau.

## 5. Backtest 3–6 tháng bằng code hiện tại

Chạy lại `fintech_bot/services/backtest.py` qua CLI hiện tại với cache historical 1d READY. Chọn FPT (HOSE), SHS (HNX), ACV (UPCOM) vì ba mã có chuỗi đủ dài, nến cuối đến **22/09/2026**, nguồn thực tế trong cả ba JSON là `VIETCAP_DIRECT`. Khoảng đánh giá **23/03/2026–22/09/2026**, 126 phiên đánh giá; mỗi kết quả dùng 252 nến đầu vào bao gồm phần trước khoảng đánh giá để khởi tạo chỉ báo. Không dùng quote live hay tải lại API cho backtest.

Giả định code giữ nguyên: vốn đầu 100.000.000 VND, phí 0,15%, trượt giá 0,05%, lô 100 cổ phiếu, giữ tối thiểu 2 phiên. Tín hiệu trên nến t được mô phỏng khớp ở **giá mở cửa nến t+1** (nếu còn nến tiếp theo). `executed_at` trong JSON là timestamp đóng của nến thực hiện, không phải giờ khớp thực tế. Equity cuối kỳ đánh dấu theo giá đóng, không ép đóng vị thế.

| Mã / sàn | BUY / SELL đã khớp | Giao dịch đóng | Final equity (VND) | Return | Max drawdown | Win rate | Vị thế mở |
|---|---:|---:|---:|---:|---:|---:|---:|
| FPT / HOSE | 1 / 1 | 1 | 92.780.437,19 | -7,22% | 10,74% | 0% | 0 |
| SHS / HNX | 1 / 1 | 1 | 90.272.692,99 | -9,73% | 16,03% | 0% | 0 |
| ACV / UPCOM | 0 / 0 | 0 | 100.000.000,00 | 0,00% | 0,00% | không xác định | 0 |

Chi tiết: [FPT](../reports/vietcap/backtest-FPT-1d.md), [SHS](../reports/vietcap/backtest-SHS-1d.md), [ACV](../reports/vietcap/backtest-ACV-1d.md); JSON cùng tên ở `reports/vietcap/`. Hai giao dịch đã đóng đều lỗ; ACV không có giao dịch nên lợi nhuận 0% **không phải bằng chứng chiến lược hiệu quả**. Mẫu ba mã và hai giao dịch quá nhỏ để kết luận thống kê hoặc ngoại suy hiệu quả đầu tư. Các report `reports/backtest-FPT-1d.*` và `reports/backtest-FPT-5m.*` là **kết quả phiên bản cũ/demo**, không dùng làm số liệu nghiệm thu hiện tại.

## 6. Đối chiếu yêu cầu đề bài

| Hạng mục | Trạng thái theo source hiện tại | Bằng chứng |
|---|---|---|
| Giá/khối lượng historical và dữ liệu hiện tại | Có nến 1d đã đóng, batch quote riêng | `fintech_bot/data/vnstock_sync.py`, `daily_cache.py`, `fintech_bot/services/live_data.py` |
| Nguồn chính/dự phòng và kiểm tra dữ liệu | KBS → Vietcap theo nguyên chuỗi từng mã, validation trước scan | `fintech_bot/data/provider_manager.py`, `validation.py`, `readiness.py` |
| Thông tin tài chính | Có cache tài chính và lệnh/điều kiện lọc | `prepare-vietcap.cmd`, `fintech_bot/services/financial_filter.py`, `fintech_bot/bot/commands.py` |
| Chiến lược BUY/SELL | EMA20/EMA50 + RSI14, chỉ 1d | `fintech_bot/strategies/indicators.py`, `ema_rsi.py` |
| Bot và cảnh báo Telegram | Có long polling, command, subscriptions, alert outbox; cần token và mạng khi chạy | `fintech_bot/bot/telegram.py`, `fintech_bot/services/alerts.py`, `fintech_bot/storage/sqlite.py` |
| Backtest 3–6 tháng nếu có | Đã chạy lại 6 tháng cho FPT, SHS, ACV | `fintech_bot/services/backtest.py`, `reports/vietcap/backtest-*-1d.{json,md}` |
| Source, hướng dẫn và kiểm thử | Có lệnh cài/chạy/test và tài liệu kiến trúc/strategy | [README](../README.md), `tests/`, [architecture.md](architecture.md), [strategy.md](strategy.md) |
| Live demo công khai | Code hỗ trợ chạy bot; chưa xác minh một bot/token/link công khai để giảng viên truy cập | `start-telegram.cmd`, `start-telegram-setup.cmd` |

## 7. Kiểm chứng và giới hạn

Bộ test project hiện có: **161 test pass** bằng `_python.cmd -B -m unittest discover -s tests -q`. Lệnh CLI và các đường dẫn tài liệu được kiểm tra trước bàn giao. Một lỗi xuất provenance backtest được sửa tối thiểu trong `fintech_bot/services/backtest.py`: report mới ghi đúng `VIETCAP_DIRECT` thay vì nhãn nguồn chung/CSV; phép tính backtest không đổi.

Giới hạn: nguồn public và độ phủ mã phụ thuộc cache đã chuẩn bị; chưa xác minh đầy đủ điều chỉnh giá do chia tách/cổ tức; nghỉ giao dịch bất thường cần cấu hình; không có nến 5m hay tín hiệu sớm; không dùng quote đang hình thành vào chỉ báo; không đặt lệnh. Mô phỏng khớp lệnh chưa xét thanh khoản thực, khớp một phần, biên độ giá, thuế bán hoặc corporate actions. Cần kiểm tra runtime Telegram và kết nối thực tế tại môi trường nộp bài, cung cấp token an toàn và link demo nếu bài nộp yêu cầu truy cập bot đang chạy. Hướng phát triển hợp lý là kiểm chứng chất lượng historical/điều chỉnh giá, mở rộng mẫu backtest và đo độ ổn định vận hành, không suy diễn từ kết quả ít giao dịch hiện tại.
