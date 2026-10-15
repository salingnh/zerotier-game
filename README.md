# ZeroTier Game Rooms

Web UI **siêu nhẹ** để tạo và xem phòng chơi game ZeroTier trên controller tự host. Người tạo phòng chỉ cần nhập **tên phòng**; ứng dụng tự tạo ZeroTier **Network ID**, chọn subnet `/24`, cấp IP động và bật broadcast cho game LAN.

- **Không** Docker, Node.js, pip/venv, database, Redis, frontend build hay dịch vụ reverse proxy mới.
- Một file ứng dụng `app.py` (Python standard library), chạy bằng systemd.
- Hiển thị **online (ước tính)**: đối chiếu member từng phòng với các peer có lưu lượng nhận gần đây qua ZeroTier local API `/peer` (mặc định 5 phút).
- Chỉ lắng nghe tại **`127.0.0.1:9394`**, giữ nguyên cấu hình Caddy đang `reverse_proxy 127.0.0.1:9394`.
- Không gửi ZeroTier `authtoken.secret` xuống trình duyệt: systemd đọc trực tiếp từ file secret gốc bằng `LoadCredential=`.
- Tự động chọn mạng từ `10.230.1.0/24` đến `10.230.254.0/24`, tránh trùng những routes/pools trên controller.
- Mạng tạo bởi portal có tên nội bộ `game-room:<tên>`; các network khác không bị hiển thị, chỉnh sửa hay xóa.
- Đơn giản hóa việc join: **public network** (người chơi chỉ cần Network ID, không phải chờ authorize).

> **Bảo mật:** Public network nghĩa là **ai biết Network ID cũng join được**. Không bật file sharing hoặc dịch vụ nhạy cảm trên network game khi chưa cấu hình firewall. Portal **không có đăng nhập nội bộ**: phải giữ Caddy Basic Auth, Cloudflare Access hoặc lớp xác thực tương đương cho toàn bộ website, bao gồm `/api/rooms`.

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

**ZeroTier Controller** phải được cài và chạy sẵn trên cùng VM. Portal không cài hoặc cấu hình lại ZeroTier One.

## Yêu cầu

- Ubuntu 24.04 ARM64 (hoặc Linux systemd có Python 3.10+ và `LoadCredential=`), `zerotier-one` đang hoạt động.
- `python3`, `curl`, `ss`, `systemctl` (thường có sẵn trên Oracle Ubuntu).
- TCP 9394 **không được có chương trình khác listen**. Nếu `zerotier-edge` còn chạy:

  ```bash
  sudo systemctl disable --now zerotier-edge
  ```

  Không cần uninstall ZeroTier Controller. Đảm bảo port trống:

  ```bash
  sudo ss -ltnp | grep 9394 || true
  ```

- DNS / Caddy hiện đang phục vụ `zero.salingnh.com`; Caddy reverse proxy tới `127.0.0.1:9394`. **Installer không chỉnh Caddyfile, không thêm/xóa firewall rule hoặc DNS.**

## Cài đặt — một lệnh sau khi tải source

SSH vào Oracle VM rồi:

```bash
curl -fL https://github.com/salingnh/zerotier-game/archive/refs/heads/main.tar.gz \
  -o /tmp/zerotier-game.tar.gz
mkdir -p /tmp/zerotier-game-src
tar -xzf /tmp/zerotier-game.tar.gz -C /tmp/zerotier-game-src --strip-components=1
cd /tmp/zerotier-game-src
sudo bash install.sh
```

Hoặc nếu đã có `git`:

```bash
git clone https://github.com/salingnh/zerotier-game.git
cd zerotier-game
sudo bash install.sh
```

Installer sẽ:

1. Kiểm tra ZeroTier One và token gốc, Python, port 9394.
2. Copy `app.py` và `uninstall.sh` vào `/opt/zerotier-game/`.
3. Tạo `/etc/default/zerotier-game` (không chứa secret).
4. Tạo, enable và start `zerotier-game.service` bằng **DynamicUser** (không tạo user hệ thống cố định).
5. Đưa token từ `/var/lib/zerotier-one/authtoken.secret` cho process qua `systemd LoadCredential=`; **không copy file token**.
6. Kiểm tra `http://127.0.0.1:9394/healthz`.

Sau khi cài:

```bash
sudo systemctl status zerotier-game --no-pager
curl -fsS http://127.0.0.1:9394/healthz
curl -fsS http://127.0.0.1:9394/api/rooms
sudo ss -ltnp | grep 9394
```

Kết quả `ss` phải bind **127.0.0.1:9394**, không phải `0.0.0.0:9394`. Mở `https://zero.salingnh.com` trên trình duyệt khi Caddy đã bảo vệ bằng đăng nhập.

Nếu Caddy vẫn còn chỉ thị `header_up X-ZT1-AUTH` của zerotier-edge cũ, portal mới không dùng header này; có thể dọn sau. Tuyệt đối không public port 9394 từ OCI Security List/NSG.

## Hướng dẫn sử dụng

1. Vào `https://zero.salingnh.com`, đăng nhập ở Caddy.
2. Nhập tên phòng, nhấn **Tạo phòng**.
3. Copy **Network ID** và gửi cho người chơi.
4. Người chơi cài [ZeroTier One](https://www.zerotier.com/download/), chọn **Join New Network**, dán ID.
5. Client tự nhận IP thuộc `10.230.x.0/24` của phòng. Game LAN có thể nhìn thấy nhau qua broadcast, phụ thuộc từng trò chơi và Windows Firewall.

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

Lệnh này xóa **service, source đã cài và cấu hình của portal**; **không** xóa ZeroTier One, identity, networks, WireGuard, Caddy, DNS hoặc rule firewall. Lý do: đây là hạ tầng dùng chung, xóa nhầm sẽ ngắt kết nối của người khác. Sau khi gỡ, Caddy cũ trỏ 9394 sẽ trả `502` cho tới khi thay proxy hay tắt site.

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
- **Không tạo được phòng:** `curl http://127.0.0.1:9394/api/rooms`, kiểm tra token ZeroTier gốc và `zerotier-one`.
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
- Network/controller state nằm ở `/var/lib/zerotier-one/`; sao lưu `identity.secret` và `controller.d/` ngoài VM theo hướng dẫn [ZeroTier Controller](https://docs.zerotier.com/controller/).
