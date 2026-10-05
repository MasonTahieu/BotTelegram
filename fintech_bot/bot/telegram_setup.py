"""Local token entry dialog. Clipboard access happens only on an explicit paste."""

import queue
import threading
from pathlib import Path

from fintech_bot.bot.telegram import TelegramClient, TelegramError, store_token


class TelegramSetupWindow:
    def __init__(self, root, path, *, client_factory=TelegramClient):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.path, self.client_factory = root, Path(path), client_factory
        self.results = queue.Queue()
        self.busy = self.closed = self.saved = False
        self.poll_job = None
        root.title("Thiết lập bot Telegram")
        root.geometry("760x600")
        root.minsize(640, 565)
        root.configure(background="#F8FAFC")
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Eyebrow.TLabel", background="#FFFFFF", foreground="#1D4ED8",
                        font=("Segoe UI", 10, "bold"))
        style.configure("Title.TLabel", background="#FFFFFF", foreground="#0F172A",
                        font=("Segoe UI", 18, "bold"))
        style.configure("Body.TLabel", background="#FFFFFF", foreground="#475569",
                        font=("Segoe UI", 10))
        style.configure("Field.TLabel", background="#FFFFFF", foreground="#0F172A",
                        font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", background="#EFF6FF", foreground="#1E40AF",
                        font=("Segoe UI", 10), padding=(14, 12))
        style.configure("Token.TEntry", padding=(12, 10), fieldbackground="#FFFFFF",
                        foreground="#17243B")
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"),
                        padding=(18, 10), background="#1D4ED8", foreground="#FFFFFF",
                        borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", "#CBD5E1"), ("active", "#1E40AF")])
        style.configure("Secondary.TButton", font=("Segoe UI", 10),
                        padding=(15, 9), background="#FFFFFF", foreground="#334155",
                        borderwidth=1, bordercolor="#CBD5E1")
        style.map("Secondary.TButton", background=[("active", "#F1F5F9")])
        style.configure("Card.TCheckbutton", background="#FFFFFF", foreground="#334155",
                        font=("Segoe UI", 10))

        panel = ttk.Frame(root, padding=(36, 28), style="Card.TFrame")
        panel.pack(fill="both", expand=True, padx=24, pady=24)
        panel.columnconfigure(0, weight=1)
        ttk.Label(panel, text="CẤU HÌNH KẾT NỐI", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        ttk.Label(panel, text="Kết nối bot Telegram", style="Title.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 10))
        ttk.Label(panel, text="Sao chép token do @BotFather cấp, rồi dán vào ô bên dưới.\n"
                              "Đây là token của bot, không phải mật khẩu tài khoản Telegram.",
                  style="Body.TLabel", wraplength=625).grid(
            row=2, column=0, sticky="w", pady=(0, 24))
        ttk.Label(panel, text="Token Telegram", style="Field.TLabel").grid(
            row=3, column=0, sticky="w")
        self.token = tk.StringVar(root)
        self.entry = ttk.Entry(panel, textvariable=self.token, show="*",
                               font=("Consolas", 12), style="Token.TEntry")
        self.entry.grid(row=4, column=0, sticky="ew", pady=(9, 12))
        self.entry.bind("<Control-a>", self.select_all)
        self.entry.bind("<Control-A>", self.select_all)
        self.entry.bind("<Return>", lambda _: self.submit())

        controls = ttk.Frame(panel, style="Card.TFrame")
        controls.grid(row=5, column=0, sticky="ew")
        self.paste_button = ttk.Button(controls, text="Dán token", command=self.paste,
                                       style="Secondary.TButton")
        self.paste_button.pack(side="left")
        self.show_token = tk.BooleanVar(root, value=False)
        self.show_button = ttk.Checkbutton(controls, text="Hiện token", variable=self.show_token,
                                           command=self.toggle_visibility, style="Card.TCheckbutton")
        self.show_button.pack(side="left", padx=18)
        self.count = tk.StringVar(root, value="Chưa nhập token")
        ttk.Label(panel, textvariable=self.count, style="Body.TLabel").grid(
            row=6, column=0, sticky="w", pady=(13, 14))
        self.token.trace_add("write", self.token_changed)

        self.status = tk.StringVar(root, value="Có thể gõ trực tiếp, nhấn Ctrl+V hoặc dùng nút Dán token.")
        ttk.Label(panel, textvariable=self.status, style="Status.TLabel",
                  wraplength=594, justify="left").grid(
            row=7, column=0, sticky="ew", pady=(0, 20))
        buttons = ttk.Frame(panel, style="Card.TFrame")
        buttons.grid(row=8, column=0, sticky="w")
        self.save_button = ttk.Button(buttons, text="Kiểm tra và lưu", command=self.submit,
                                      style="Primary.TButton")
        self.save_button.pack(side="left")
        ttk.Button(buttons, text="Đóng", command=self.close,
                   style="Secondary.TButton").pack(side="left", padx=12)
        ttk.Label(panel, text="Chỉ kiểm tra thông tin bot và lưu trên máy; bước này chưa gửi tin nhắn.",
                  style="Body.TLabel", wraplength=625).grid(
            row=9, column=0, sticky="w", pady=(24, 0))
        self.entry.focus_set()

    def select_all(self, _=None):
        self.entry.selection_range(0, "end")
        return "break"

    def toggle_visibility(self):
        self.entry.configure(show="" if self.show_token.get() else "*")

    def token_changed(self, *_):
        count = len(self.token.get().strip())
        self.count.set(f"Đã nhập {count} ký tự" if count else "Chưa nhập token")

    def paste(self):
        if self.busy:
            return
        try:
            content = self.root.clipboard_get()
        except Exception:
            self.status.set("Chưa lấy được nội dung đã sao chép. Hãy sao chép token rồi bấm Dán token lần nữa.")
            return
        self.token.set(content.strip())
        self.entry.focus_set()
        self.entry.icursor("end")
        self.status.set("Đã dán vào ô nhập. Bấm Kiểm tra và lưu để tiếp tục.")

    def set_busy(self, value):
        self.busy = value
        for widget in (self.entry, self.paste_button, self.save_button, self.show_button):
            widget.state(["disabled"] if value else ["!disabled"])

    def submit(self):
        if self.busy or self.closed:
            return
        token = self.token.get().strip()
        if not token:
            self.status.set("Bạn chưa nhập token. Hãy dán token từ BotFather vào ô ở trên.")
            self.entry.focus_set()
            return
        try:
            client = self.client_factory(token)
        except ValueError:
            self.status.set("Token chưa đúng định dạng. Hãy chỉ sao chép chuỗi token từ BotFather, không kèm lời nhắn.")
            return
        self.set_busy(True)
        self.show_token.set(False)
        self.toggle_visibility()
        self.status.set("Đang kiểm tra bot qua Telegram… Cửa sổ vẫn có thể đóng nếu bạn muốn hủy.")
        threading.Thread(target=self.verify, args=(client, token), daemon=True).start()
        self.poll_job = self.root.after(100, self.poll)

    def verify(self, client, token):
        try:
            profile = client.verify()
            self.results.put((True, token, profile))
        except TelegramError as error:
            self.results.put((False, "", str(error)))
        except Exception:
            # A third-party/OS exception can contain a credential-bearing URL.
            self.results.put((False, "", "Chưa kiểm tra được bot. Kiểm tra kết nối mạng rồi thử lại."))

    def poll(self):
        self.poll_job = None
        if self.closed:
            return
        try:
            success, token, result = self.results.get_nowait()
        except queue.Empty:
            self.poll_job = self.root.after(100, self.poll)
            return
        self.set_busy(False)
        if not success:
            self.status.set(result + " Token cũ trên máy chưa bị thay đổi.")
            return
        try:
            store_token(token, self.path)
        except OSError:
            self.status.set("Bot đã được xác nhận nhưng chưa lưu được token. Kiểm tra quyền ghi thư mục dự án rồi thử lại.")
            return
        self.saved = True
        self.token.set("")
        self.status.set(f"Đã lưu thành công cho @{result['username']}.\n"
                        "Bạn có thể đóng cửa sổ này, chạy start-telegram.cmd rồi mở bot và gửi /start.")

    def close(self):
        self.closed = True
        if self.poll_job is not None:
            self.root.after_cancel(self.poll_job)
        self.token.set("")
        self.root.destroy()


def setup_window(path):
    try:
        import tkinter as tk
    except ImportError:
        raise ValueError("Python thiếu cửa sổ nhập liệu. Cài thành phần Tcl/Tk của Python hoặc chạy: "
                         "python -B -m fintech_bot setup-telegram --console") from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise ValueError("Không mở được cửa sổ nhập token. Chạy trong cửa sổ lệnh: "
                         "python -B -m fintech_bot setup-telegram --console") from None
    dialog = TelegramSetupWindow(root, path)
    root.mainloop()
    return 0  # Closing before saving is cancellation, not a token error.
