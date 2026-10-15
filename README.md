# ZeroTier Game Rooms

Web UI **siêu nhẹ** để tạo và xem phòng chơi game ZeroTier trên controller tự host. Người tạo phòng chỉ cần nhập **tên phòng**; ứng dụng tự tạo ZeroTier **Network ID**, chọn subnet `/24`, cấp IP động và bật broadcast cho game LAN.

- **Không** Docker, Node.js, pip/venv, database, Redis, frontend build hay dịch vụ reverse proxy mới.
- Một file ứng dụng `app.py` (Python standard library), chạy bằng systemd.
- Hiển thị **online (ước tính)**: đối chiếu member từng phòng với các peer có lưu lượng nhận gần đây qua ZeroTier local API `/peer` (mặc định 5 phút).
- Chỉ lắng nghe tại **`127.0.0.1:9394`**, giữ nguyên cấu hình Caddy đang `reverse_proxy 127.0.0.1:9394`.
- Hỗ trợ controller key từ file `.env` bảo mật hoặc tự đọc `authtoken.secret` của ZeroTier One (tương thích bản cũ). systemd dùng `LoadCredential=`: key **không** xuất hiện trong HTML, source hoặc biến môi trường của process.
- Tự động chọn mạng từ `10.230.1.0/24` đến `10.230.254.0/24`, tránh trùng những routes/pools trên controller.
- Mạng tạo bởi portal có tên nội bộ `game-room:<tên>`; các network khác không bị hiển thị, chỉnh sửa hay xóa.
- Đơn giản hóa việc join: **public network** (người chơi chỉ cần Network ID, không phải chờ authorize).

> **Bảo mật:** Public network nghĩa là **ai biết Network ID cũng join được**. Không bật file sharing hoặc dịch vụ nhạy cảm trên network game khi chưa cấu hình firewall. Portal **không có đăng nhập nội bộ**: phải giữ Caddy Basic Auth, Cloudflare Access hoặc lớp xác thực tương đương cho toàn bộ website, bao gồm `/api/rooms`. **Không commit `.env` hoặc controller token lên GitHub.**

## Kiến trúc

```text
Browser --> https://zero.salingnh.com
                    |
                    v
         Caddy (authentication + TLS)
                    |
                    v
          127.0.0.1:9394 (Python)
                    |
                    v
          127.0.0.1:9993 (ZeroTier Controller)

WireGuard vpn.salingnh.com / UDP 51820: không thay đổi
ZeroTier P2P / UDP 9993: không thay đổi
```

**ZeroTier Controller** có thể chạy trên cùng VM (mặc định `http://127.0.0.1:9993`) hoặc trên máy khác qua API HTTPS. Portal không tự cài hoặc cấu hình lại ZeroTier One. Sơ đồ là cấu hình controller nội bộ mặc định; khi dùng controller từ xa, thay tuyến API bằng URL HTTPS đã đặt trong `.env`.

## Yêu cầu

- Ubuntu 24.04 ARM64 hoặc Linux có systemd `LoadCredential=` và Python 3.10+.
- `python3`, `curl`, `ss`, `systemctl`.
- ZeroTier controller tự host chạy **cùng máy** hoặc trên **máy khác** có API HTTPS và token hợp lệ.
- Cổng TCP **9394** trống, hoặc đang do chính `zerotier-game.service` sử dụng.
- Caddy reverse-proxy đến `127.0.0.1:9394`, bắt buộc có xác thực cho cả web và `/api/rooms`.

Nếu `zerotier-edge` cũ còn chiếm 9394, hãy dừng riêng nó (không dừng ZeroTier One):

```bash
sudo systemctl disable --now zerotier-edge
sudo ss -ltnp | grep 9394 || true
```

## Cài đặt và cấu hình controller key

### Bước 1 — Tải source

Ưu tiên dùng Git để dễ nâng cấp:

```bash
git clone https://github.com/salingnh/zerotier-game.git
cd zerotier-game
```

Hoặc dùng tarball nếu không cài Git:

```bash
curl -fL https://github.com/salingnh/zerotier-game/archive/refs/heads/main.tar.gz \
  -o /tmp/zerotier-game.tar.gz
mkdir -p /tmp/zerotier-game-src
tar -xzf /tmp/zerotier-game.tar.gz -C /tmp/zerotier-game-src --strip-components=1
cd /tmp/zerotier-game-src
```

### Bước 2 — Tạo `.env` chứa controller key (khuyến nghị)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Nội dung mẫu (thay giá trị thứ hai bằng **token thật**, không commit):

```dotenv
ZT_GAME_CONTROLLER_URL=http://127.0.0.1:9993
ZT_GAME_CONTROLLER_TOKEN=YOUR_ZEROTIER_SERVICE_API_TOKEN
```

- `ZT_GAME_CONTROLLER_URL`: URL API của ZeroTier controller. **Cùng máy** dùng `http://127.0.0.1:9993`. Nếu controller ở **máy khác** thì dùng HTTPS có chứng chỉ hợp lệ, bảo vệ bằng firewall/reverse proxy; không công khai cổng 9993.
- `ZT_GAME_CONTROLLER_TOKEN`: **ZeroTier One Service API token**. Trên Linux chạy ZeroTier One, lấy bằng lệnh `sudo cat /var/lib/zerotier-one/authtoken.secret`. Đây **không phải** Network ID, `identity.secret` hay ZeroTier Central API key.
- Định dạng `KEY=value` (không thêm `export`; cho phép quote giá trị). `.env` đã được bỏ khỏi Git bởi `.gitignore`; vẫn phải `chmod 600 .env`.

**Tương thích bản cũ:** Nếu app và ZeroTier One cùng máy, bạn **có thể bỏ qua bước 2**. Khi không có `.env`, installer tiếp tục lấy token nội bộ bằng systemd `LoadCredential=zerotier-token:...`, không copy token.

### Bước 3 — Cài đặt service

```bash
sudo bash install.sh
```

Installer:

1. Kiểm tra Python, port 9394 và cấu hình credential.
2. Nếu có `.env` và chưa có cấu hình cài đặt, copy sang **`/etc/zerotier-game/controller.env`**, quyền **root:root `0600`**, thư mục `0700`.
3. Dùng systemd **`LoadCredential=`** để truyền file .env vào process, không dùng `EnvironmentFile=` cho secret. Nếu không có .env, tiếp tục dùng token local như trước.
4. Cài `/opt/zerotier-game/`, giới hạn không nhạy cảm tại `/etc/default/zerotier-game` và bật `zerotier-game.service`.
5. Kiểm tra cả `/healthz` **và** `/api/rooms` để phát hiện key/URL sai. Installer không sửa Caddy, DNS, WireGuard, firewall hoặc controller networks.

**Khi nâng cấp**, nếu đã tồn tại `/etc/zerotier-game/controller.env`, installer **không ghi đè** bằng `.env` ở source. Để thay/rotate key, hãy sửa file trên host rồi restart:

```bash
sudo nano /etc/zerotier-game/controller.env
sudo chmod 600 /etc/zerotier-game/controller.env
sudo systemctl restart zerotier-game
```

**Không** đặt key vào `/etc/default/zerotier-game`; file này dành cho các thông số không nhạy cảm.

### Bước 4 — Kiểm tra triển khai

```bash
sudo systemctl status zerotier-game --no-pager
curl -fsS http://127.0.0.1:9394/healthz
curl -fsS http://127.0.0.1:9394/api/rooms
sudo ss -ltnp | grep 9394
```

`ss` phải thấy service bind **`127.0.0.1:9394`**, không phải `0.0.0.0:9394`. Nếu `/healthz` trả OK nhưng `/api/rooms` trả 502, kiểm tra controller URL, key và `sudo journalctl -u zerotier-game -n 50 --no-pager`.

Mở **`https://zero.salingnh.com`** nếu Caddy đã được cấu hình `reverse_proxy 127.0.0.1:9394` và xác thực toàn bộ site. **Không public port 9394** qua OCI firewall/Security List/NSG.

### Nâng cấp từ bản cũ

```bash
cd zerotier-game
git pull --ff-only
sudo bash install.sh
```

Room, trạng thái controller và Caddy cũ vẫn được giữ. Nếu đang chạy bản không có `.env`, fallback sang token nội bộ vẫn hoạt động. Muốn chuyển sang .env, làm bước 2 rồi cài lại. Source `.env` không được tự động xóa hoặc đưa vào Git.


## Vận hành và kiểm tra Web UI

Sau khi triển khai, quản trị viên mở `https://zero.salingnh.com` qua lớp xác thực Caddy, tạo thử một phòng và xác nhận Network ID được cấp tự động. Khi dùng API, kiểm tra `GET /api/rooms` để xác nhận quyền truy cập controller và danh sách mạng.

**Phân tách tài liệu:**
- **README này dành cho quản trị viên triển khai Web UI:** tải source, đặt controller URL/key vào `.env`, cài service systemd, xác thực qua Caddy, nâng cấp và xử lý lỗi.
- **Hướng dẫn cài ZeroTier Client và join room dành cho người chơi nằm ngay trên Web UI:** nhấn **Cách join** cạnh phòng mong muốn. Web UI tự điền đúng Network ID và lệnh join Linux; nội dung bao gồm link tải client chính thức, hướng dẫn Windows/macOS/Android/iOS/Linux và cách kiểm tra kết nối.

Website không thể tự cài ZeroTier Client hoặc tự join thay người chơi. Chỉ cung cấp Network ID cho người muốn tham gia; tuyệt đối không gửi controller API key cho người chơi.

Danh sách hiển thị **Room ID/Network ID**, tên và **số node online (ước tính)**. Portal gọi `/peer` **một lần mỗi lần làm mới**, chỉ tính `LEAF` có path đang hoạt động và `lastReceive` trong cửa sổ thời gian gần đây, rồi đối chiếu Node ID với danh sách member của từng phòng. Giao diện tự cập nhật mỗi **10 giây**.

**Giới hạn:** `/peer` là trạng thái peer của **node controller trên toàn bộ ZeroTier**, không cung cấp presence riêng cho từng mạng. Thiết bị online ở mạng khác nhưng đã join phòng có thể được tính vào phòng; ngược lại, thiết bị đang trao đổi P2P nhưng lâu không liên lạc controller có thể không được tính. Đây là số **ước tính**, không khẳng định người chơi đang mở game, cũng không xóa member khi offline.

Mạng game **không có default route**, vì vậy không giành vai trò Internet Exit Node với WireGuard hiện có.

## API tối thiểu

| API | Ý nghĩa |
|---|---|
| `GET /healthz` | Kiểm tra HTTP service |
| `GET /api/rooms` | Liệt kê các mạng có tiền tố `game-room:`, kèm `online` (ước tính) |
| `POST /api/rooms` | Tạo phòng: JSON `{"name":"AOE tối nay"}` |

Ví dụ từ chính VM (API chưa có xác thực riêng, không chạy lệnh này từ Internet nếu chưa qua Caddy auth):

```bash
curl -fsS http://127.0.0.1:9394/api/rooms
curl -fsS http://127.0.0.1:9394/api/rooms \
  -H 'Content-Type: application/json' \
  -d '{"name":"AOE tối nay"}'
```

**Mặc định giới hạn** 64 phòng và 10 lượt tạo phòng/giờ trên toàn ứng dụng (bộ đếm lượt tạo lưu RAM và reset khi service restart). Không có tự xóa phòng theo thời gian trong V1; nên có quy trình dọn phòng khi cần.

`ZT_GAME_ONLINE_WINDOW_MINUTES=5` (cho phép 1–60 phút) điều chỉnh khoảng thời gian tính **online ước tính**. Trường `online` trong JSON `GET /api/rooms` và `POST /api/rooms` thay cho trường `members` cũ; giá trị mới không phải tổng số thiết bị từng join. Trên máy đã cài bản cũ, có thể tự thêm dòng này vào `/etc/default/zerotier-game` hoặc giữ mặc định 5 phút.

Chỉnh giới hạn tại `/etc/default/zerotier-game`, sau đó:

```bash
sudo systemctl restart zerotier-game
```

## Gỡ cài đặt hoàn toàn ứng dụng

Script gỡ được **cài kèm**, vì vậy bạn có thể xóa source sau khi cài:

```bash
sudo bash /opt/zerotier-game/uninstall.sh
```

Hoặc từ bản source:

```bash
sudo bash uninstall.sh
```

Lệnh này xóa **service, source đã cài, `/etc/default/zerotier-game` và `/etc/zerotier-game/controller.env` (secret đã cài)**; **không** xóa `.env` trong source checkout, ZeroTier One, identity, networks, WireGuard, Caddy, DNS hoặc rule firewall. Lý do: đây là hạ tầng dùng chung, xóa nhầm sẽ ngắt kết nối của người khác. Sau khi gỡ, Caddy cũ trỏ 9394 sẽ trả `502` cho tới khi thay proxy hay tắt site.

Nếu **cố ý muốn xóa cả những network do portal tạo** (tên bắt đầu `game-room:`), sử dụng trước khi xóa source:

```bash
sudo bash /opt/zerotier-game/uninstall.sh --purge-rooms
```

Script sẽ bắt nhập `DELETE` để xác nhận thao tác không thể hoàn tác. Các network tạo thủ công hoặc không có prefix `game-room:` vẫn được giữ nguyên. **Hãy backup controller trước** nếu đang có người chơi.

## Sự cố thường gặp

```bash
sudo systemctl status zerotier-game --no-pager
sudo journalctl -u zerotier-game -n 50 --no-pager
sudo systemctl status zerotier-one --no-pager
sudo ss -ltnp | grep -E ':(9394|9993)\b'
```

- **Port 9394 bận:** `zerotier-edge` vẫn còn chạy. Stop Edge, không stop ZeroTier One.
- **Trang `zero.salingnh.com` trả 502:** kiểm tra `zerotier-game` active và Caddy có `reverse_proxy 127.0.0.1:9394`.
- **Caddy không khởi động:** kiểm tra Caddyfile và các drop-in EnvironmentFile cũ của zerotier-edge (đặc biệt nếu đã xóa `/etc/caddy/zerotier-edge.env`). Portal mới không cần cấu hình chèn controller token vào Caddy.
- **Không tạo được phòng hoặc API 502:** `curl http://127.0.0.1:9394/api/rooms`; kiểm tra `sudo journalctl -u zerotier-game -n 50 --no-pager`, controller URL/key tại `/etc/zerotier-game/controller.env` (hoặc token gốc khi dùng fallback). Controller remote cần HTTPS và phải truy cập được từ host chạy portal.
- **Số online luôn 0 hoặc không đúng:** xem `sudo zerotier-cli -j peers` (hoặc `sudo zerotier-cli peers`) trên máy controller. Nếu peer không có `lastReceive` gần đây thì portal không thể xác định online, dù các thiết bị vẫn có thể đang kết nối P2P.
- **Không thấy LAN game:** kiểm tra Windows Firewall, virtual adapter, broadcast; ZeroTier Layer 2 tạo điều kiện LAN discovery nhưng không cam kết tương thích 100% với mọi bản game.

## Kiểm thử phát triển (không phải cài lên VM)

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh uninstall.sh
```

Không có dependency bên ngoài; repo dùng GitHub Actions để chạy unit tests và kiểm tra shell scripts.

## Phạm vi và an toàn

- Không có đăng nhập nội bộ: **KHÔNG trực tiếp expose 9394**; Caddy Basic Auth phải bao phủ toàn bộ route và API.
- Room public không phù hợp làm mạng quản trị hoặc lưu dữ liệu riêng tư; tạo network `private: true` độc lập cho những mục đích đó.
- Nếu port 9394 đang bị chiếm bởi phần mềm khác, installer **từ chối cài**, không tự kill hoặc gỡ phần mềm không thuộc ứng dụng.
- Installer/uninstaller không thay đổi `wg0`, rules NAT, ZeroTier identity, Caddy, các Network ID khác hay tài nguyên OCI.
- Network/controller state trên host tự chạy ZeroTier nằm ở `/var/lib/zerotier-one/`; sao lưu `identity.secret` và `controller.d/` ngoài VM theo hướng dẫn [ZeroTier Controller](https://docs.zerotier.com/controller/).
- File controller key chỉ root có quyền đọc. Người có quyền `sudo/root` trên host vẫn có thể đọc key. Với controller remote, luôn dùng HTTPS/TLS hợp lệ và giới hạn truy cập vào API.
