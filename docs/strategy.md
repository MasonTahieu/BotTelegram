# Chiến lược EMA20/EMA50 + RSI14

Đây là chiến lược kỹ thuật đang chạy trong project, dựa trên giá đóng cửa của **nến ngày 1d đã đóng**. Cài đặt mặc định ở [config.py](../fintech_bot/config.py) và [configs/vietcap.toml](../configs/vietcap.toml); công thức ở [indicators.py](../fintech_bot/strategies/indicators.py), điều kiện tín hiệu ở [ema_rsi.py](../fintech_bot/strategies/ema_rsi.py). Các ngưỡng trong tài liệu này mô tả code hiện tại, không phải ngưỡng được tối ưu từ backtest.

## EMA20 và EMA50

EMA là trung bình động đặt trọng số lớn hơn lên giá gần đây. Gọi Close[t] là giá đóng cửa phiên t, N là số phiên và alpha là hệ số làm mượt:

~~~text
alpha = 2 / (N + 1)
EMA[N-1] = SMA của N giá đóng cửa đầu tiên
EMA[t] = alpha × Close[t] + (1 − alpha) × EMA[t-1]    (t >= N)
~~~

Code dùng N = 20 cho EMA nhanh và N = 50 cho EMA chậm. Trước khi đủ N giá, giá trị EMA là None. EMA20 phản ứng với biến động mới nhanh hơn EMA50; một giao cắt mới giữa hai đường là điều kiện cần để phát BUY hoặc một trong hai điều kiện phát SELL. Chỉ việc EMA20 tiếp tục nằm trên EMA50 không tạo BUY lặp lại.

## Wilder RSI14

RSI so sánh mức tăng và giảm của giá đóng cửa. Với từng phiên kể từ phiên thứ hai:

~~~text
ΔPrice[t] = Close[t] − Close[t-1]
Gain[t]   = max(ΔPrice[t], 0)
Loss[t]   = max(−ΔPrice[t], 0)
~~~

Với N = 14, Average Gain và Average Loss đầu tiên là trung bình của 14 mức tăng/giảm đầu tiên. Sau đó code làm mượt theo Wilder:

~~~text
Average Gain[t] = (Average Gain[t-1] × (N − 1) + Gain[t]) / N
Average Loss[t] = (Average Loss[t-1] × (N − 1) + Loss[t]) / N
RS[t]           = Average Gain[t] / Average Loss[t]
RSI[t]          = 100 − 100 / (1 + RS[t])
~~~

RSI đầu tiên xuất hiện sau 14 biến động giá, tức tại giá đóng cửa thứ 15; các vị trí trước đó là None. Code xử lý các trường hợp không thể chia thông thường: Average Gain = Average Loss = 0 thì RSI = 50; Average Loss = 0 còn Average Gain > 0 thì RSI = 100; Average Gain = 0 còn Average Loss > 0 thì RSI = 0.

## Điều kiện tín hiệu tại nến cuối

Gọi t là phiên đóng gần nhất và t−1 là phiên ngay trước đó. [EmaRsiStrategy.evaluate](../fintech_bot/strategies/ema_rsi.py) trả về một trong ba trạng thái:

| Trạng thái | Điều kiện |
|---|---|
| BUY | EMA20[t−1] ≤ EMA50[t−1], EMA20[t] > EMA50[t] **và** RSI14[t] > 50 |
| SELL | EMA20[t−1] ≥ EMA50[t−1] **và** EMA20[t] < EMA50[t]; **hoặc** RSI14[t−1] > 70 **và** RSI14[t] ≤ 70 |
| NONE | Không có điều kiện BUY/SELL mới tại nến cuối |

Code xét SELL trước BUY. Nếu điều kiện thoát và vào cùng xuất hiện ở một phiên, kết quả là SELL. RSI giảm khi vẫn ở trên 70 chưa đủ tạo SELL theo nhánh RSI; nó phải đi từ trên 70 xuống mức 70 hoặc thấp hơn. Code chưa có điều kiện thủng hỗ trợ, chốt lời hay cắt lỗ.

Với cấu hình mặc định, chiến lược cần ít nhất 51 giá đóng cửa để có EMA50 ở cả t−1 và t. Luồng historical yêu cầu tối thiểu 52 nến khi đánh giá dữ liệu sẵn sàng. [Scanner](../fintech_bot/services/scanner.py) kiểm tra thứ tự phiên, OHLCV và mốc đóng; nến thiếu, cũ hoặc không hợp lệ không phát tín hiệu mới. Scanner chỉ chuyển **nến ngày đã đóng** vào chỉ báo. Vietcap live quote dùng cho hiển thị trong phiên, không ghép vào chuỗi historical của KBS hoặc Vietcap để tính EMA/RSI. Runtime hiện chỉ hỗ trợ 1d.

## Bộ lọc tùy chọn và ý nghĩa tín hiệu

Mỗi người dùng có thể đặt các ngưỡng ROE tối thiểu, P/E tối đa, P/B tối đa, nợ/vốn tối đa, EPS tối thiểu và Amihud ILLIQ tối đa. [FinancialFilter](../fintech_bot/services/financial_filter.py) dùng dữ liệu tài chính hoặc 20 mức sinh lời từ 21 nến ngày đã đóng để đánh giá; mã thiếu dữ liệu cần thiết không qua điều kiện đã bật. Lệnh /screen liệt kê mã đạt bộ lọc, chưa phải tín hiệu BUY.

Bộ lọc được áp dụng sau khi chiến lược kỹ thuật tạo tín hiệu. Nó có thể chặn **BUY** khỏi /signals và cảnh báo theo từng người dùng; **SELL không bị chặn** bởi bộ lọc. Vì vậy tín hiệu kỹ thuật được lưu và điều kiện một người dùng nhận được thông báo là hai việc khác nhau. Các ngưỡng lọc do người dùng tự chọn, mặc định chưa bật.

Backtest trong [backtest.py](../fintech_bot/services/backtest.py) dùng nến đã đóng và giả định tín hiệu tại nến t được thực hiện ở giá mở cửa nến t+1 nếu có nến tiếp theo. BUY/SELL là nhãn của quy tắc kỹ thuật để nghiên cứu, **không phải khuyến nghị đầu tư** hay cam kết khớp lệnh thực tế.
