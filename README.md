# Quản lý thư viện — GitHub Pages + Render

## 1. Cấu trúc
- `frontend/`: website HTML/CSS/JS chạy GitHub Pages
- `backend/app.py`: REST API Flask + SQLite, giữ cấu trúc database và nghiệp vụ cũ
- `render.yaml`: Render Blueprint, dùng **dịch vụ trả phí có persistent disk**
- `.github/workflows/pages.yml`: tự xuất bản frontend lên GitHub Pages

## 2. Đưa lên GitHub
Tạo repository (ví dụ `quan-ly-thu-vien`) và tải toàn bộ thư mục dự án lên nhánh `main` (không đưa database và secret.key lên GitHub).
Vào **Settings → Pages → Build and deployment → Source: GitHub Actions**. Workflow tự publish thư mục `frontend`.
URL ví dụ: `https://TEN_GITHUB.github.io/quan-ly-thu-vien/`.

## 3. Render backend
Vào Render → New → Blueprint, chọn repository có `render.yaml`.
Blueprint yêu cầu gói hỗ trợ **persistent disk**; không dùng SQLite với dịch vụ Render miễn phí để lưu dữ liệu thật.
Cấu hình biến môi trường:
- `ADMIN_PASSWORD`: mật khẩu quản trị khởi tạo, **ít nhất 12 ký tự**; chỉ có hiệu lực khi DB chưa có admin.
- `FRONTEND_ORIGIN`: `https://TEN_GITHUB.github.io` (chỉ origin, KHÔNG thêm `/quan-ly-thu-vien/`)
- `DATA_DIR`: `/var/data` (đã cấu hình trong render.yaml)
Dịch vụ khởi động bằng `gunicorn app:app --workers 1`.

Nếu tạo Web Service thủ công: Root directory=`backend`; Build=`pip install -r requirements.txt`; Start=`gunicorn app:app --workers 1 --bind 0.0.0.0:$PORT`; gắn persistent disk tại `/var/data`.

## 4. Kết nối
Mở trang GitHub Pages, nhập URL Render đầy đủ, ví dụ `https://library-api-xxxx.onrender.com`, không thêm `/api`. Trang sẽ lưu URL trên trình duyệt.
Tài khoản khởi tạo: `admin`, mật khẩu là `ADMIN_PASSWORD` đã cấu hình.

## 5. Chạy thử cục bộ
Trong `backend`: `pip install -r requirements.txt`.
Linux/macOS: `export ADMIN_PASSWORD='a-strong-password-123'; export FRONTEND_ORIGIN='http://localhost:8000'; python app.py`.
Windows PowerShell: `$env:ADMIN_PASSWORD='a-strong-password-123'; $env:FRONTEND_ORIGIN='http://localhost:8000'; python app.py`.
Trong `frontend`: `python -m http.server 8000`.
Lần đầu mở frontend, nhập `http://127.0.0.1:5000` làm URL backend.

## 6. Lưu ý bảo mật và vận hành
- SQLite và `secret.key` nằm trên persistent disk; tuyệt đối không commit lên GitHub.
- Các phiên đăng nhập hết hạn sau 8 giờ. Token lưu trong sessionStorage của tab trình duyệt.
- Chỉ một tài khoản admin, chưa có rate limiting/nhật ký kiểm toán/backup tự động.
- Thay đổi `ADMIN_PASSWORD` sau khi database đã được tạo không thay mật khẩu trong DB; dùng màn hình Đổi mật khẩu.
- Xóa đầu sách/tập có thể xóa lịch sử mượn theo ON DELETE CASCADE, như phần mềm gốc.
- Nên sao lưu SQLite định kỳ và bổ sung giám sát/rate limiting trước khi dùng công khai quy mô lớn.
- `backend/original_app.py` là bản tham chiếu mã nguồn gốc, không phải entrypoint triển khai.
