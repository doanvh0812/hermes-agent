# Bảng quản trị web cho bot Zalo

Giao diện thao tác trực quan thay cho việc sửa `allowlist.json` bằng tay và
chạy `zalo_allow.py` trong terminal.

| Tab | Làm được gì |
|---|---|
| Người dùng | Danh bạ kèm tên và số điện thoại · cho dùng / chặn / bỏ đánh dấu · phong và thu quyền quản trị · đổi chế độ `friends` ↔ `list` |
| Nhóm | Danh sách nhóm kèm số thành viên · duyệt / thu quyền — thay cho việc phải gõ `/duyet-nhom` trong chính nhóm đó |
| Đăng nhập Zalo | Trạng thái phiên · tạo mã QR, hiện ngay trên trang, và link để quét từ điện thoại |
| Nhật ký | `audit.jsonl` dạng bảng, lọc được theo từ khoá |

## Nó không tự làm lấy thứ gì

Toàn bộ là giao diện đặt lên máy móc đã có:

- **Danh bạ, nhóm, đăng nhập QR** — gọi API loopback của bridge
  (`/friends`, `/groups`, `/qr/start`, `/qr/status`).
- **Quyền truy cập** — dùng chính `AllowlistStore` mà adapter dùng, nên sửa ở
  đây thì gateway đang chạy nhận trong **5 giây**, không cần khởi động lại và
  không sinh ra nguồn dữ liệu thứ hai.
- **Nhật ký** — đọc `audit.jsonl`.

## Bảo mật

Trang này **cấp và thu quyền dùng bot, và khởi động được đăng nhập QR** — ai
vào được thì chiếm được tài khoản Zalo của bot. Vì vậy:

- **Chỉ bind `127.0.0.1`.** Truyền `--host 0.0.0.0` sẽ bị **từ chối chạy**, không
  phải cảnh báo. Đây cũng là lựa chọn mà dashboard của chính Hermes đã chốt
  trong đợt siết tháng 06/2026, khi cờ `--insecure` bị biến thành no-op.
  TLS phải kết thúc ở reverse proxy — xem `deploy/admin/Caddyfile.example`.
- **Bắt buộc có mật khẩu**, băm bằng `scrypt` của stdlib. Không có hash trong
  `.env` thì tiến trình không khởi động.
- **Session** là cookie ký HMAC, `HttpOnly` + `SameSite=Strict`, và bật `Secure`
  khi proxy báo `X-Forwarded-Proto: https`.
- **CSRF token** bắt buộc trên mọi request thay đổi trạng thái.
- **Chặn dò mật khẩu**: 8 lần sai trong 5 phút thì khoá theo IP.

Đường `/qr` và `/qr/status` **cố ý không đòi session** — đó là link đưa cho điện
thoại, mà điện thoại thì không đăng nhập trang quản trị. Bản thân token trong
URL là chứng chỉ: bridge sinh 24 byte ngẫu nhiên, hết hạn sau 10 phút, và cháy
ngay khi đăng nhập xong. Mọi đường khác của bridge vẫn chỉ loopback.

Link QR trỏ về **chính trang quản trị** (`/qr` proxy sang bridge), nên điện thoại
đi qua TLS của proxy và **không cần mở cổng 8647 ra Internet**.

## Cài đặt

```powershell
$env:HERMES_HOME = 'C:\Users\Administrator\AppData\Local\hermes\profiles\zalo-bot'
$py = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"

# 1. Đặt mật khẩu (chỉ hash được lưu)
& $py server.py --set-password

# 2. Chạy thường trú
& ..\deploy\admin\register-admin-task.ps1
Start-ScheduledTask -TaskName HermesZaloAdmin

# 3. HTTPS: xem deploy/admin/Caddyfile.example
```

Không cần cài thêm thư viện: FastAPI, uvicorn, Jinja2 và httpx đã có sẵn trong
venv của Hermes.

## Ba cái bẫy đã gặp khi dựng

Ghi lại vì cả ba đều làm tiến trình chết hoặc trả 500 mà thông báo không chỉ
đúng chỗ:

1. **`hashlib.scrypt` với `n=2**15` vượt `maxmem` mặc định của OpenSSL** (đúng
   32 MB) → `ValueError: memory limit exceeded`. Phải truyền `maxmem` tường minh.
2. **Starlette 1.x đổi chữ ký `TemplateResponse`** thành `(request, name, context)`.
   Dạng cũ `(name, {"request": ...})` khiến Jinja nhận dict làm tên template →
   `TypeError: unhashable type: 'dict'`.
3. **`AllowlistStore.mode` là `@property`**, không phải method → `'str' object is
   not callable`. Còn `remove_admin` thì **ném `ValueError`** cho admin cuối cùng
   và **trả `False`** khi id không phải admin — hai ý nghĩa trái ngược nhau, phải
   tách ra thành 409 và 404.
