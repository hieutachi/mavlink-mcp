# SafeMAV — Audit ban đầu của mavlink-mcp

Ngày đánh giá: **05/11/2026**. Trạng thái: **đã audit; chưa triển khai SafeMAV**.

## 1. Kết luận để quyết định đầu tư

**Nên tiếp tục trên repository hiện có, nhưng không coi guardrails hiện tại là một security gateway.** Capability layer, adapter interface, MCP stdio, mock fixtures và simulator nội bộ là nền tảng phù hợp. Không cần viết lại MAVSDK backend hoặc đổi tên package.

Khoảng cách lớn nhất không phải thêm một hộp thoại xác nhận. Đó là: xác thực chủ thể; tách quyền ký duyệt khỏi agent; ràng buộc chính xác dữ liệu được duyệt với dữ liệu thực thi; chống replay có lưu bền; kiểm tra trạng thái tại thời điểm thực thi; bảo vệ mọi đường gọi adapter; và đưa ra bằng chứng đúng mức.

Hai kết luận có điều kiện rất quan trọng:

1. Có thể chặn MCP client độc hại và lời gọi Python qua API được bảo vệ bằng gateway quyết định tất định.
2. **Không thể bảo đảm chống Python tùy ý chạy trong cùng interpreter/quyền hệ điều hành với adapter chỉ bằng class/private attribute/token nội bộ.** Cam kết mạnh hơn đòi hỏi cách ly tiến trình, quyền truy cập khóa và mạng MAVSDK/MAVLink, sau đó kiểm thử chính cấu hình triển khai đó.

Khuyến nghị phạm vi cuộc thi: một UAV PX4 SITL, approval CLI độc lập, bộ T01–T25 cùng positive controls, benchmark baseline và log thật. Hoãn dashboard, nhiều UAV, ArduPilot, hardware và nghiên cứu novelty sâu cho đến khi các gate cốt lõi đạt.

## 2. Mốc mã nguồn và phạm vi đã đọc

### 2.1 Hai baseline không được trộn lẫn

- GitHub `main` được kiểm tra bằng `gh repo view` và `gh api`: `fb0324d7298da2f9659780a223fb79bd6747b6ef`.
- Local HEAD cũng ở commit trên, branch `main`, nhưng **working tree không sạch**.
- Git history có hai commit: `962448c` — scaffold MVP1; `fb0324d` — tài liệu project memory.
- Có sẵn trước audit: 7 tệp tracked đã sửa và 4 tệp untracked. Không phải thay đổi do lượt audit này tạo ra.

| Trạng thái trước audit | Tệp |
| --- | --- |
| Modified | `README.md`, `docs/simulator-quickstart.md`, `src/mavlink_mcp/drone.py`, `src/mavlink_mcp/guardrails.py`, `src/mavlink_mcp/server.py`, `tests/test_guardrails.py`, `tests/test_server_tools.py` |
| Untracked | `examples/patrol_sim.py`, `src/mavlink_mcp/adapters/sim_adapter.py`, `tests/test_sim_adapter.py`, `tests/test_stdio_sim.py` |

Trong báo cáo này, số dòng là của **working tree tại thời điểm audit**, không phải số dòng trên GitHub. [AUDIT_BASELINE.json](AUDIT_BASELINE.json) lưu SHA-256 của 31 tệp gốc, trạng thái Git và phiên bản dependency. Hash là dấu mốc so sánh, không phải chữ ký bảo đảm nguồn gốc.

### 2.2 Nội dung đã kiểm tra

Đã đọc trọn 31 tệp hiện có thuộc phạm vi repository: README, SECURITY, ROADMAP, CHANGELOG, CONTRIBUTING, CLAUDE, LICENSE, `.gitignore`, `pyproject.toml`; toàn bộ 10 tệp Python trong `src/`; toàn bộ 8 tệp trong `tests/` (kể cả `__init__.py` rỗng); tài liệu simulator; và 3 tệp example. Đã xem Git history, local diff và metadata GitHub. Không kiểm toán toàn bộ mã nguồn của các dependency hoặc `.venv`.

Không chạy `examples/patrol_sitl.py`, không khởi động PX4, không gọi các connector MAVLink/ROS đang cấu hình trong ứng dụng, không chạm UAV thật. Chỉ chạy suite có sẵn và các probe in-memory với `MockAdapter`; không triển khai chức năng mới.

### 2.3 Kiểm tra đã thực sự chạy

```text
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider
93 passed, 1 warning in 6.55s

.venv/Scripts/python.exe -B -m pip check
No broken requirements found.
```

Cảnh báo: `AuthlibDeprecationWarning` từ `fastmcp/server/auth/providers/jwt.py:10`, báo `authlib.jose` deprecated. Không được gọi lượt chạy này là không có cảnh báo. `pip check` chỉ kiểm tra tương thích dependency đã cài, không phải quét CVE hoặc license.

Suite có một subprocess stdio dùng simulator nội bộ. Đây là kiểm thử MCP thực qua stdio với chuyển động synthetic, **không phải SITL**. Chưa chạy bộ test của commit GitHub trong môi trường tách biệt; con số 93 chỉ áp dụng cho working tree local.

| Thành phần | Phiên bản thực tế đã quan sát |
| --- | --- |
| Python | 3.13.3, Windows AMD64 |
| fastmcp / mcp | 2.14.7 / 1.29.0 |
| mavsdk / grpcio | 3.17.2 / 1.83.0 |
| pydantic / cryptography | 2.13.4 / 50.0.0 |
| pytest / pytest-asyncio | 8.4.2 / 1.4.0 |

`cryptography` hiện có trong môi trường không có nghĩa project đã khai báo hoặc sử dụng nó cho human approval. API Python MAVSDK đã cài được introspect: `Battery.remaining_percent` ở bản 3.17.2 là **0–100**. Không tự áp dụng quy ước 0–1 của phiên bản/tài liệu khác.

## 3. Kiến trúc hiện tại và mọi đường lệnh đã tìm thấy

```text
MCP stdio -> server.py: 12 tool functions
                         |
Python library ----------+-> Drone
                               | capability + readonly + confirm + limits
                               v
                           DroneAdapter
                           /          \
                    MavsdkAdapter   SimAdapter (local only)
                         |
                    MAVSDK System -> gRPC/MAVLink -> autopilot

Python trực tiếp -> drone.adapter / MavsdkAdapter -> bypass Drone
MAVSDK/MAVLink peer khác -----------------------> bypass cả project
```

- Entry point: `pyproject.toml:49–50`; `__main__.py:3–6`; `server.py:356–363`.
- Backend: `server.py:52–70`; chọn một lần, backend lạ bị từ chối, không fallback. Mặc định vẫn là MAVSDK; `main()` chỉ tạo backend, không tự connect.
- Tool registration: `server.py:339–353`; 4 read tools và 8 mutation tools.
- Capability checks và guardrails: `drone.py:64–143`, các mutation tại `168–243`.
- Public adapter access: `drone.py:46–48`.
- Chỉ `adapters/mavsdk_adapter.py` trong source project import MAVSDK.
- `examples/patrol_sitl.py:45–85` dùng Drone + MAVSDK và tự bật write/confirm; là một đường lệnh vật lý tiềm năng, không chứng minh endpoint là simulator.
- `examples/patrol_sim.py:19–29,32–104` tạo MCP subprocess chỉ với backend sim.

### 3.1 Tám thao tác cấp cao, mười điểm ghi backend

| Thao tác | MCP -> Drone | Điểm ghi MAVSDK (`adapters/mavsdk_adapter.py`) |
| --- | --- | --- |
| arm | `server.arm` -> `Drone.arm` | `204–205`: `action.arm` |
| disarm | `server.disarm` -> `Drone.disarm` | `207–208`: `action.disarm` |
| takeoff | `server.takeoff` -> `Drone.takeoff` | `210–216`: **`set_takeoff_altitude` rồi `takeoff`** |
| land | `server.land` -> `Drone.land` | `218–219`: `action.land` |
| hold | `server.hold` -> `Drone.hold` | `221–222`: `action.hold` |
| return_to_launch | `server.return_to_launch` -> `Drone.return_to_launch` | `224–228`: `action.return_to_launch` |
| goto_location | `server.goto_location` -> `Drone.goto` | `230–250`: `action.goto_location` |
| fly_mission | `server.fly_mission` -> `Drone.fly_mission` | `252–261`: **`upload_mission` rồi `start_mission`** |

Upload mission và đặt độ cao takeoff cũng là ghi trạng thái backend. Không chỉ instrument các lệnh làm UAV bắt đầu bay. Connect, get_home và telemetry là I/O quan sát, cần đếm riêng khỏi physical writes.

## 4. Tính năng được mô tả so với bằng chứng

| Tính năng | Kết luận từ code/test |
| --- | --- |
| Readonly mặc định | Có; 8 mutation của Drone bị chặn; test còn assert không gọi adapter. Không bao phủ raw adapter. |
| Arm/takeoff confirmation | Có kiểm tra Boolean; **không có xác thực người duyệt**. |
| Trần độ cao, geofence | Có kiểm tra target/waypoint. Không giám sát quỹ đạo liên tục, không chứng minh tránh địa hình/vật cản; mutable-list bypass tại F03. |
| Input finite, tọa độ, speed, mission length | Working tree có bổ sung thực chất; GitHub HEAD chưa có các thay đổi này. Vẫn thiếu strict type/schema đồng nhất. |
| 12 MCP tools / Python facade | Có implementation và tests. |
| MAVSDK adapter | Có implementation và smoke tests không kết nối xe; không có test sống của PX4 trong repo. |
| Simulator nội bộ | Có ở local untracked; 93-test run có kiểm tra kinematic/stdio. Pin/GPS null; không thay thế PX4. |
| PX4 “primary tested target” trong README | Là claim trong tài liệu, **không có run artifact trong repository để audit xác nhận**. Không kết luận chưa bao giờ được tác giả thử. |
| ArduPilot/hardware | Chưa được xác minh; ROADMAP đã để ArduPilot validation là việc còn lại. |
| Approval ký số, nonce/expiry, identity | Chưa triển khai. Không gọi “token replay vulnerability” như thể đã có token. |
| Policy engine/state machine/monitor/audit | Chưa triển khai theo nghĩa SafeMAV; hiện chỉ các điều kiện guardrail cục bộ. |

## 5. Findings có thể hành động

Severity dưới đây là ưu tiên engineering định tính trong bối cảnh write-enabled simulator/gateway; không phải CVSS, không phải công bố rằng đã khai thác UAV thật. Mỗi finding phân biệt lỗi hiện hữu, giới hạn thiết kế và thiếu bằng chứng.

### F01 — Boolean confirmation không phải human authorization

- **Mức:** High; thiếu security control so với SafeMAV.
- **Vị trí:** `drone.py:80–87,168–187`; `server.py:170–213`; README `105–106`.
- **Kịch bản:** client có quyền gọi tool, server write-enabled, tự gửi `confirm=true`; không có thông tin operator/key được kiểm tra.
- **Bằng chứng:** probe MockAdapter trả `['connect', 'arm']` với `Drone.arm(confirm=True)`. Existing tests `test_capability_layer.py:41–48` cũng cố ý xác nhận hành vi này.
- **Tác động:** agent tự đáp ứng điều kiện confirmation; tool description chỉ là lời hướng dẫn.
- **Sửa đề xuất:** principal tin cậy, permission riêng, approval Ed25519 ngoài agent, exact binding, TTL, nonce lưu bền. Boolean không được cấp quyền.
- **Test nghiệm thu:** T01–T07; cả MCP và Python; không token hoặc token sai phải có zero physical writes. Một request hợp lệ thực thi đúng một lần.

### F02 — Raw adapter là đường vòng ngoài facade

- **Mức:** High, **phụ thuộc trust boundary**; low-level interface là thiết kế hiện có, không phải bypass qua MCP được chứng minh.
- **Vị trí:** `drone.py:46–48`; `adapter.py:74–104`; `adapters/mavsdk_adapter.py:204–261`; README `140–141`.
- **Kịch bản:** Python caller truy cập `drone.adapter` hoặc khởi tạo `MavsdkAdapter` thay vì dùng Drone.
- **Bằng chứng:** trên MockAdapter, facade readonly nhưng `await drone.adapter.arm()` vẫn ghi `('arm',)`.
- **Tác động:** “mọi Python API đều được bảo vệ” không đúng với phạm vi hiện tại.
- **Đường vòng dependency đã kiểm tra:** MAVSDK 3.17.2 có sidecar gRPC; `.venv/Lib/site-packages/mavsdk/system.py:136` mặc định port 50051, `async_plugin_manager.py:29` dùng `aio.insecure_channel`. Đây là quan sát mã client, không phải kiểm tra live bind/exposure của server. Chỉ khóa UDP mà để agent chạm gRPC vẫn không đủ.
- **Sửa đề xuất:** Drone công khai đi qua gateway; bỏ trả raw adapter ở facade, thay bằng descriptor/read metadata. Raw implementation ở trusted runtime. Nếu adversary có Python tùy ý, phải cách ly quyền/mạng, gồm gRPC/MAVLink/serial và quyền spawn service; không coi đổi tên `_adapter` là phòng vệ.
- **Test nghiệm thu:** T20a facade không token bị chặn; T20b legacy/raw method không execution lease hợp lệ bị chặn trong supported API; T20c sandbox Python không thể đến gRPC/MAVLink hoặc đọc khóa. Không đạt T20c thì không công bố bảo đảm chống arbitrary-code bypass.

### F03 — TOCTOU: danh sách mission thay đổi sau validation

- **Mức:** High; đã tái hiện bằng mock, không kết nối xe.
- **Vị trí:** `drone.py:225–243`, đặc biệt validation trước `await _ensure_connected()` và `_check_geofence()`; `Waypoint` frozen nhưng `list` không frozen.
- **Kịch bản:** caller giữ tham chiếu list; một coroutine đổi phần tử trong khi connect/home đang await. Geofence sau đó chỉ kiểm khoảng cách, không lặp lại altitude validation.
- **Bằng chứng:** list ban đầu một waypoint 10 m; Mock.connect thay phần tử thành waypoint cùng vị trí 999 m; trần 50 m; log `connect,get_home,fly_mission`, altitude gửi là **999**.
- **Tác động:** dữ liệu gửi không phải dữ liệu vừa kiểm tra, dù readonly đã tắt hợp lệ.
- **Sửa đề xuất:** deep-copy/normalize thành immutable tuple ngay trước await đầu tiên; hash và thực thi cùng một object đã đóng băng; không đọc lại list của caller.
- **Test nghiệm thu:** test cố tình đổi list ở connect, get_home, chờ lock và upload. Chỉ dữ liệu snapshot hợp lệ được dùng, hoặc request bị từ chối; tuyệt đối không gửi dữ liệu mới ngoài giới hạn.

### F04 — Flight health/preconditions chủ yếu là lời nhắc

- **Mức:** High; thiếu safety policy.
- **Vị trí:** `drone.py:168–243`; `capabilities.py:70–84`; `server.py:173–176,203–204,263–264`.
- **Kịch bản:** gửi arm/takeoff/goto khi thiếu GPS/pin, không đúng armed/in_air/mode. Drone không fetch/check health trước các action.
- **Bằng chứng:** probe set mock `in_air=True, armed=True, battery=None, gps=None, mode=UNKNOWN`, `disarm()` vẫn tới adapter; telemetry không được đọc.
- **Tác động:** phụ thuộc autopilot từ chối, không phải gateway tự ngăn. Không được giả định mọi firmware đều từ chối giống nhau.
- **Sửa đề xuất:** policy theo action và target PX4 profile đã pin; unknown/null là unknown, không “healthy”. Sim thiếu sensor phải có profile synthetic được dán nhãn riêng.
- **Test nghiệm thu:** T16–T17, T24–T25; thêm takeoff chưa arm, navigation khi grounded, GPS/pin null và threshold boundaries.

### F05 — Connection flag và snapshot không chứng minh freshness

- **Mức:** High; thiếu kiểm tra runtime.
- **Vị trí:** `adapters/mavsdk_adapter.py:104–138,147–181`; `capabilities.py:70–84`.
- **Kịch bản:** link mất sau lần discovery đầu; `_connected` vẫn true. Các trường lấy bằng nhiều stream riêng, mỗi field timeout 5 giây; không có tuổi từng field, thời điểm home hay epoch reconnect.
- **Tác động:** trạng thái “connected” và “consistent snapshot” không đủ để cấp quyền hành động. Một giá trị trả nhanh có thể là cache của SDK; receipt timestamp không chứng minh freshness của sensor.
- **Sửa đề xuất:** subscriber/liveness task tin cậy, tuổi từng trường bằng monotonic clock, connection epoch, skew bound, health supervisor; ghi rõ nếu chỉ đo receive-freshness.
- **Test nghiệm thu:** T16, T18, T23; link flapping, frozen source stream, một field stale còn field khác fresh; không đo TTL bằng đồng hồ hệ thống có thể lùi.

### F06 — Không có serialization hay snapshot policy cho mỗi thao tác

- **Mức:** High; nguy cơ từ cấu trúc code, chưa chạy race backend thật.
- **Vị trí:** `drone.py:50–54,71–143,168–243`; `adapters/mavsdk_adapter.py:107–134`.
- **Kịch bản:** hai request arm/takeoff/mission/recovery xen kẽ qua await; connect có thể diễn ra đồng thời; môi trường được đọc lại ở nhiều check.
- **Tác động:** policy/approval/state tại lúc kiểm tra có thể khác lúc phát lệnh; chưa có “một operation đang hoạt động”.
- **Sửa đề xuất:** một writer/vehicle, connect lock riêng, immutable policy snapshot+version, request lifecycle và final recheck. Lock phải bounded; recovery không được chết đói sau một MAVSDK call treo.
- **Test nghiệm thu:** T19, T21–T22; concurrency với fake barrier/clock, token dùng đồng thời, đổi policy trong khi chuẩn bị, command timeout/cancellation.

### F07 — Home/altitude có thể không cùng reference frame

- **Mức:** High, tình huống cần fault-injection xác minh.
- **Vị trí:** `drone.py:109–139,220–223`; `adapters/mavsdk_adapter.py:230–250`.
- **Kịch bản:** geofence dùng lần đọc home A; `MavsdkAdapter.goto()` tự đọc home B để đổi relative sang absolute altitude. Home có thể đổi hoặc absolute altitude không hợp lệ.
- **Tác động:** target vật lý không cùng anchor vừa được policy phê chuẩn. Hiện chỉ kiểm tra lat/lon home tại facade, không kiểm finite home.absolute_altitude_m trong adapter.
- **Sửa đề xuất:** freeze home identity/epoch và target absolute altitude trong execution plan; invalidate khi home/connection thay đổi; không để adapter tự chọn anchor mới sau validation.
- **Test nghiệm thu:** home A/B khác nhau, absolute altitude NaN/Inf, home reset sau approval; zero writes cho request đã invalid.

### F08 — “Recovery luôn an toàn” và readonly chưa phân vai

- **Mức:** High; safety design gap.
- **Vị trí:** `drone.py:175–205`; `server.py:185–195,216–254`; tests `test_guardrails.py:66–70`, `test_sim_adapter.py:158–168`.
- **Kịch bản:** agent không cần confirmation để land/RTL/hold; hoặc UAV đang bay, readonly bật và mọi recovery tool của agent cũng bị khóa. Disarm không kiểm grounded ở facade.
- **Tác động:** land sai chỗ, RTL không phù hợp GPS/home hoặc disarm airborne không thể được coi là giảm nguy hiểm tự động. Readonly chặn agent là hợp lý nhưng chưa có recovery authority tách biệt.
- **Sửa đề xuất:** giữ readonly với agent; operator/supervisor principal riêng và recovery policy theo trạng thái. Không “sửa” bằng cho mọi client bypass readonly; không disarm airborne; không dùng kill process làm emergency procedure.
- **Test nghiệm thu:** T25 theo matrix grounded/airborne/unknown, GPS mất, link mất, audit/monitor lỗi; chứng minh recovery không trở thành cửa hậu.

### F09 — Policy cấu hình không fail-closed hoàn toàn

- **Mức:** Medium; chủ yếu operator/configuration error, không suy diễn MCP client sửa được env.
- **Vị trí:** `guardrails.py:41–100`; `drone.py:33–35,50–54`.
- **Kịch bản:** nhập sai số được thay bằng default thay vì báo cấu hình invalid; `0` chủ động tắt giới hạn; `readonly` khi tạo dataclass trực tiếp không strict bool.
- **Tác động:** cấu hình thực dùng khác dự định; khó bind approval với policy ổn định. Local đã sửa NaN/Inf/negative/underflow fallback nên không được báo lại như lỗi local còn nguyên.
- **Sửa đề xuất:** SafeMAV có policy schema strict immutable, startup invalid thì không flight, version/hash; profile được bảo vệ không cho tắt fence/ceiling bằng 0. Quyết định migration phải công khai.
- **Test nghiệm thu:** T22, missing/unknown key, bool-as-number, nonfinite, typo, reload invalid; current valid policy không bị thay một phần.

### F10 — Input validation/error contract còn lệch giữa hai API

- **Mức:** Medium; robustness, chưa chứng minh arbitrary malformed input gây physical bypass.
- **Vị trí:** `guardrails.py:23–28`; `server.py:78–87,198–213,257–336`; `capabilities.py:87–94`.
- **Kịch bản:** Python Boolean được `math.isfinite` coi là số; direct `Drone.takeoff(True,confirm=True)` gửi True tới MockAdapter. Một số `float()` cast của server chỉ được catch bằng `_HANDLED`, khác với parser mission.
- **Tác động:** type annotations không đủ; API Python và MCP transport có thể nhận/xử lý lỗi khác nhau. FastMCP có validation riêng nhưng chưa thay thế invariant ở core.
- **Sửa đề xuất:** một strict normalizer dùng chung; reject bool/nonfinite, unknown fields, giới hạn payload; reason codes ổn định và no raw traceback/secrets.
- **Test nghiệm thu:** T12 trên transport thật lẫn Python; payload quá lớn, thiếu field, NaN/Inf, bool, string số, unknown metadata. Không gộp generic error với thành công chặn flight.

### F11 — Action acknowledgment không đủ cho trạng thái hoàn tất

- **Mức:** High cho thiết kế thực thi; chưa có replay token để đánh giá hiện tại.
- **Vị trí:** `adapters/mavsdk_adapter.py:210–216,252–269`; `server.py:182,195,206–213,240,336`.
- **Kịch bản:** set_takeoff_altitude/upload thành công nhưng takeoff/start timeout; cancel task sau khi backend nhận lệnh; client tự thử lại vì chỉ thấy error.
- **Tác động:** kết quả không rõ hoặc tác động một phần, có thể phát lệnh lặp. Không thể rollback giao dịch vật lý bằng rollback SQLite.
- **Sửa đề xuất:** request state ACCEPTED/IN_PROGRESS/COMPLETED/FAILED/UNKNOWN tách policy decision; durable one-use trước write; không tự retry UNKNOWN; instrument từng substep.
- **Test nghiệm thu:** timeout/cancel/crash trước và sau từng write sink; nonce không sống lại; UNKNOWN cần đối chiếu telemetry/operator chứ không thực thi lại.

### F12 — Không có audit trail bảo mật

- **Mức:** Medium; evidence/control gap.
- **Vị trí:** `server.py:78–84` chỉ dựng error dict; mutation handlers và `adapters/mavsdk_adapter.py:263–269` không có durable security event.
- **Kịch bản:** không chứng minh ai yêu cầu/duyệt, tham số nào được gửi, denied request có tới adapter không.
- **Tác động:** khó điều tra, không đo audit completeness/replay đúng nghĩa; tool response không phải audit độc lập.
- **Sửa đề xuất:** durable event+nonce transaction từ đầu, hash chain và checkpoint bên ngoài tùy phạm vi; không log token nguyên vẹn/private key.
- **Test nghiệm thu:** mỗi request có decision và terminal/unknown event; sửa/xóa/reorder log bị phát hiện dưới mô hình đã công bố. Hash chain riêng không chống attacker viết lại cả DB hoặc xóa suffix nếu không có checkpoint.

### F13 — Dependency/build chưa được khóa để tái lập

- **Mức:** Medium; không phải CVE đã xác nhận.
- **Vị trí:** `pyproject.toml:1–3,38–47`; inventory không có lockfile/CI workflow.
- **Kịch bản:** cài mới chọn version khác trong range lớn, đặc biệt MAVSDK 2/3 và transitive stack; Python support được khai báo 3.10–3.13 nhưng chỉ môi trường 3.13.3 được kiểm tra lượt này.
- **Tác động:** benchmark không tái lập, semantics sensor/API có thể đổi. `pip check` pass không chứng minh an toàn chuỗi cung ứng hoặc toàn bộ license permissive.
- **Sửa đề xuất:** giữ library ranges hợp lý nhưng chốt environment lock cho demo/benchmark; khai báo trực tiếp crypto dùng; scan advisory/license bằng công cụ phù hợp khi triển khai; ghi timestamp và nguồn scan.
- **Test nghiệm thu:** clean install trong môi trường pin, package import + full mock tests; kiểm tra phiên bản MAVSDK khác trước khi claim tương thích.

### F14 — Bằng chứng SITL/documentation chưa đủ

- **Mức:** Medium; reproducibility và safety communication.
- **Vị trí:** README `135–141`; `tests/test_mavsdk_adapter.py:1–5,21–62`; `examples/patrol_sitl.py:30–42,51–62`; simulator guide `45–67`.
- **Kịch bản:** người dùng hiểu smoke tests là đã validate PX4. Example có wait không deadline, đọc battery/GPS không guard None; endpoint env không chứng minh là SITL.
- **Tác động:** demo treo hoặc kết nối nhầm; claim vượt chứng cứ. Không đánh đồng 93 test pass với đủ chất lượng cho hardware.
- **Sửa đề xuất:** runner SITL tách biệt, timeout, target identity và network isolation, artifact firmware/image digest, outcome thật; tài liệu trạng thái implemented/tested/unverified.
- **Test nghiệm thu:** clean-room PX4 run có raw trace + flight state + failure cases, no real endpoint; các thiếu hụt công khai trong KNOWN_LIMITATIONS.

## 6. Ghi chú lịch sử: GitHub chưa có hardening local

`git diff` cho thấy HEAD chỉ kiểm `altitude_m <= 0` và so với ceiling, chưa có `finite_number`; chưa kiểm lat/lon/yaw, speed và giới hạn 1000 waypoint như local. `_env_float` cũ chỉ `float(raw)` rồi trả về. Đây là lý do phải giữ local hardening và tách baseline benchmark, không “sửa lại từ GitHub” làm mất phần việc đang có.

Audit **không chạy** exploit NaN/Inf trên bản GitHub, nên chỉ kết luận sự khác biệt code và thiếu check nhìn thấy, không điền tỷ lệ tấn công thành công cho baseline đó.

## 7. Backward compatibility

Giữ `mavlink_mcp`, console script `mavlink-mcp`, đơn vị vật lý, tên 12 tool và adapter translation nơi có thể. Các thay đổi an toàn cần breaking behavior có chủ đích:

- `confirm=true` không còn đủ để được arm/takeoff; chữ ký hợp lệ thay quyền cấp bởi Boolean.
- Không có execution context thì direct Python mutation bị deny; telemetry vẫn usable.
- Public facade không đưa raw adapter; server metadata chuyển sang read-only descriptor.
- Test cũ khẳng định “confirm là đủ”, “land/hold/RTL vô điều kiện” phải migration có lý do; không xóa âm thầm để đạt màu xanh.
- Strict normalization, frozen policy, response lifecycle mới cần migration notes; không giữ chế độ legacy bypass trong SafeMAV protected runtime.
- Giữ SimAdapter và các test untracked; chỉ đổi health profile một cách minh bạch, không bịa pin/GPS cho simulator.

## 8. Thứ tự ưu tiên

1. Freeze baseline, chốt threat boundary/claim và test instrumentation.
2. Immutable schema + strict policy + request/vehicle identity; sửa F03 trước khi thêm crypto.
3. Gateway duy nhất; atomic nonce + audit intent; Ed25519 và CLI operator độc lập.
4. Per-vehicle serialization, expiry/recheck tại 10 sinks, timeout/cancel UNKNOWN.
5. Telemetry freshness, home binding, state machine, bounded recovery, monitor health.
6. T01–T25 và test bổ sung, protected deployment, baseline benchmark, PX4 demo.
7. Report số liệu thật và submission; dashboard chỉ sau gate cốt lõi.

Audit trail/intent phải có cùng anti-replay transaction, không đẩy toàn bộ audit xuống “Priority 3” như master prompt. UI vẫn là ưu tiên thấp.

## 9. Thông tin cuộc thi đã đối chiếu

Trang chính thức được truy cập ngày 05/11/2026: [Prevent Irreversible Autonomous Actions](https://www.drawerrr.com/challenge/ai-safety-and-control/prevent-irreversible-autonomous-actions).

Nội dung truy xuất xác nhận hạn **15/11/2026**, cho phép existing projects, các trọng số **35/25/20/15/5**, và hạng mục **Best in safeguarding physical-world actions**. Múi giờ đóng cổng và chi tiết biểu mẫu submit **chưa xác minh**. Không coi 23:59 giờ Việt Nam là hạn chắc chắn; nên sẵn sàng trước 14/11.

Không dự đoán giải thưởng hay điểm số. “Physical-world relevance” phải dựa vào đường điều khiển PX4 có kiểm chứng, dù thử nghiệm chỉ ở SITL; không cần và không nên bay nguy hiểm bằng phần cứng thật.

Nguồn bổ trợ cho thiết kế: [Ed25519 — cryptography](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/), [MAVSDK Battery](https://mavsdk.mavlink.io/main/en/cpp/api_reference/structmavsdk_1_1_telemetry_1_1_battery.html). Với API cụ thể, kết quả introspection của version pin được ưu tiên hơn suy đoán từ tài liệu phiên bản khác.

## 10. Bàn giao

Đọc tiếp [THREAT_MODEL.md](THREAT_MODEL.md), [ARCHITECTURE.md](ARCHITECTURE.md), rồi [SAFEMAV_IMPLEMENTATION_PLAN.md](SAFEMAV_IMPLEMENTATION_PLAN.md). Những tài liệu này là **đề xuất và kế hoạch chưa thực thi**, không phải mô tả chức năng đã có.
