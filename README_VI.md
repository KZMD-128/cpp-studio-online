# C++ Studio Online — phương án B (GitHub Pages + GNU G++/GDB backend)

Dự án giữ lại giao diện và bộ mô phỏng RAM JavaScript của C++ Studio hiện tại. Nút **G++ + mô phỏng** gửi mã nguồn đến API trên Linux VPS; mỗi yêu cầu được chạy trong một Docker container dùng một lần, không có mạng và có giới hạn tài nguyên.

## Kiến trúc

- `frontend/`: website tĩnh xuất bản bằng GitHub Pages.
- `backend/api/`: API nhận code, kiểm tra yêu cầu và khởi tạo runner.
- `backend/runner/`: image chứa GNU G++, GDB và Python để biên dịch/chụp trạng thái thực thi.
- `docker-compose.yml`: chạy API trên VPS; runner image được build riêng.

## 1. Chuẩn bị

Cần một Linux VPS riêng (khuyến nghị Ubuntu 24.04 x64), Docker Engine + Docker Compose plugin, và một tên miền cho API, ví dụ `api.example.com`. GitHub Pages chỉ host frontend; nó không chạy G++/GDB.

Không dùng máy chủ chia sẻ không cho chạy Docker. Không đưa backend này lên Internet trước khi cấu hình HTTPS, giới hạn truy cập và lớp chống lạm dụng.

## 2. Cấu hình frontend

Sửa `frontend/config.js`:

```js
window.CPP_STUDIO_API_BASE = "https://api.example.com";
```

Không thêm dấu `/` ở cuối. Đổi `api.example.com` thành tên miền API thực tế.

## 3. Cấu hình backend trên VPS

Đưa repository lên VPS, sau đó:

```bash
cp .env.example .env
mkdir -p /opt/cpp-studio/jobs
```

Sửa `.env`:

- `JOBS_HOST_DIR=/opt/cpp-studio/jobs`
- `CPP_STUDIO_ALLOWED_ORIGINS=https://YOUR_GITHUB_USERNAME.github.io`

Nếu dùng GitHub Pages dạng project site, origin vẫn là `https://YOUR_GITHUB_USERNAME.github.io` (không thêm `/repository`). Nếu dùng tên miền riêng cho frontend, thêm origin đó vào danh sách, phân tách bằng dấu phẩy.

Build runner image chứa G++/GDB:

```bash
docker build -t cpp-studio-runner:latest ./backend/runner
```

Khởi động API:

```bash
docker compose up -d --build api
```

Kiểm tra nội bộ trên VPS:

```bash
curl http://127.0.0.1:8080/api/health
```

Kết quả `ready: true` cho biết runner image đã sẵn sàng.

## 4. Bật HTTPS cho API

Cài Caddy trên VPS và cấu hình một virtual host tương tự `Caddyfile.example`:

```caddy
api.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
}
```

Trỏ bản ghi DNS `A` của `api.example.com` về IP VPS, mở cổng 80/443 và khởi động Caddy. Caddy sẽ cấp TLS tự động. Không mở cổng 8080 trực tiếp ra Internet; Compose chỉ bind API vào `127.0.0.1`.

## 5. Xuất bản giao diện bằng GitHub Pages

1. Tạo repository GitHub và đưa toàn bộ thư mục dự án vào nhánh `main`.
2. Trong repository, vào **Settings → Pages**.
3. Chọn **GitHub Actions** làm Build and deployment source.
4. Push commit lên `main`; workflow `.github/workflows/pages.yml` sẽ xuất bản thư mục `frontend/`.
5. Mở URL Pages được GitHub cung cấp.

## Giới hạn mặc định của bản thử nghiệm

- Mã nguồn tối đa 2 MB; stdin tối đa 256 KB.
- Tối đa 2 tác vụ đồng thời trên một API instance.
- Giới hạn mặc định 8 yêu cầu/phút/IP.
- Container chạy code: không mạng, filesystem gốc chỉ đọc, 512 MB RAM, 1 CPU, tối đa 64 tiến trình, thời gian tác vụ giới hạn.
- GDB dùng `SYS_PTRACE` chỉ bên trong runner container riêng.

## Cảnh báo bảo mật quan trọng

Người dùng có thể gửi mã C++ tùy ý, bao gồm vòng lặp vô hạn, fork bomb và chương trình cố khai thác lỗ hổng kernel. Docker container là lớp cô lập cơ bản, không phải ranh giới bảo mật tuyệt đối. API container cần quyền truy cập Docker socket; vì vậy VPS phải là máy chủ chuyên dụng cho dịch vụ này, không chứa dữ liệu cá nhân hoặc dịch vụ nhạy cảm.

Trước khi mở công khai rộng rãi, cần bổ sung tối thiểu: reverse proxy HTTPS, Cloudflare/WAF hoặc CAPTCHA (ví dụ Turnstile), giới hạn theo IP/tài khoản ở tầng proxy, giám sát tài nguyên, cập nhật Docker/kernel thường xuyên và cân nhắc runtime cô lập mạnh hơn như gVisor hoặc microVM. CORS không phải cơ chế xác thực và không ngăn được gọi API bằng curl.

## Kiểm thử

- `GET /api/health`: kiểm tra API và runner image.
- Nút **G++ + mô phỏng**: gọi `/api/trace`, biên dịch bằng G++ và lấy các snapshot từ GDB.
- Bộ mô phỏng nhanh hiện tại vẫn chạy ở frontend như trước; nó không thay thế kết quả thực thi G++/GDB.
