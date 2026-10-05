HISTORICAL BOOTSTRAP - PATCH/RESUME

Trang thai sau khi doc archive Du an 3(8):
- Universe: 1523
- Canonical historical READY sau khi tan dung toan bo cache hop le dang co: 400
- STALE canonical: 65
- MISSING canonical: 1058
- Checkpoint: completed 400, failed da xu ly 311, chua xu ly 812

Da sua nho logic resume trong fintech_bot/data/vnstock_sync.py:
- Resume binh thuong KHONG goi API lai cac ma da failed trong cung ngay.
- No van thu migrate cache local moi neu co.
- --retry-failed la lenh rieng de thu lai cac ma da failed.
- Khong them service/manager/worker moi.

Cach dung tren may Windows:
1. Tat Telegram bot.
2. Backup thu muc Du an 3 hien tai.
3. Giai nen patch vao GOC thu muc Du an 3 va cho phep overwrite.
4. Chay: prepare-history.cmd
5. De job chay den het. Khong can mo GPT Work; day la lenh Python chay tren may, khong ton usage ChatGPT.
6. Khi xong, doc dong tong ket Universe/READY/ERROR.
7. Neu muon thu lai rieng nhung ma failed sau luot dau: prepare-history.cmd --retry-failed
8. Sau do khoi dong lai Telegram bot.

Luu y:
- Khong ep ERROR ve 0. Nhieu ma UPCOM/it thanh khoan co the khong co nen o phien gan nhat va hop ly bi STALE/khong du dieu kien phat signal.
- Khong merge live quote vao historical/EMA/RSI.
- Patch nay khong chua file token Telegram.
