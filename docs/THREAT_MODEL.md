# SafeMAV — Threat model đề xuất

Ngày: 05/11/2026. **Thiết kế chưa được triển khai hoặc chứng nhận.** Hiện trạng và bằng chứng: [INITIAL_SECURITY_AUDIT.md](INITIAL_SECURITY_AUDIT.md).

## 1. Mục tiêu và hai cấp bảo đảm

**G1 — API enforcement:** với gateway/runtime/config/keystore tin cậy, MCP client hoặc Python caller qua supported public API không được làm một lệnh mới tới physical write sink nếu thiếu quyền, approval hợp lệ hoặc safety precondition.

**G2 — Deployment containment:** một agent có arbitrary Python trong sandbox riêng không thể bỏ gateway để gọi raw adapter, MAVSDK gRPC, MAVLink UDP/TCP/serial, sửa policy/ledger hoặc tự ký approval.

G1 cần kiểm thử core, adapter và transport. G2 cần kiểm thử deployment với quyền/network thực tế. G1 **không suy ra G2**. Module private, opaque Python object hay một process khác nhưng cùng quyền/cùng filesystem chưa đủ chứng minh G2.

Không đưa ra cam kết “ngăn mọi tai nạn” hoặc “UAV không bao giờ rời fence”. Gateway giảm các action không được cấp phép trong phạm vi đã kiểm chứng; autopilot còn có dynamics, failsafes, state và các nguồn điều khiển khác.

## 2. Assets

| Asset | Thuộc tính cần bảo vệ |
| --- | --- |
| Quyền điều khiển vehicle | Chỉ gateway/supervisor/operator được cấp quyền mới phát lệnh |
| Operator signing key | Agent và MCP service không được đọc hoặc ký thay người |
| Operator identity/trust store | Không nhận khóa mới do request tự khai; quyền operator do cấu hình tin cậy quyết định |
| Agent principal | Lấy từ transport/launcher, không lấy `actor_id` trong tool arguments |
| Exact request/mission | Immutable, versioned, digest bao gồm mọi tham số ảnh hưởng execution |
| Policy | Strict, immutable snapshot, có hash/epoch; agent không sửa hoặc hạ cấp |
| Vehicle target/home/frame | Mapping được bảo vệ; epoch thay đổi hủy request/lease cũ |
| Nonce ledger và request state | Durable, một lần sử dụng ở cấp gateway; không hồi sinh sau restart/restore |
| Telemetry/liveness | Tuổi dữ liệu, nguồn và uncertainty rõ; unknown không đồng nghĩa safe |
| Audit/evidence | Đủ trace, chống sửa ở mức công bố; không lộ private key/raw token |

## 3. Chủ thể và giả định

| Chủ thể | Quyền/giả định |
| --- | --- |
| Malicious/compromised LLM | Có thể gọi mọi tool được công bố, đổi thứ tự/params, gửi prompt injection và `confirm=true`; không mặc định có OS shell của gateway |
| Malicious MCP client | Có thể bỏ qua prompt, sửa/gửi lại token, tạo concurrent requests, disconnect ở thời điểm bất lợi |
| Agent có Python | Trong G2 chạy sandbox với pipe MCP; không có network/serial/device/Docker socket đến trusted stack |
| Human operator | Giữ private key, đọc action từ snapshot CLI, chỉ ký khi chủ động; có thể từ chối. Việc người đọc không cẩn thận là residual risk |
| Gateway runtime | Trusted code/config/DB, sở hữu raw adapter; không thực thi plugin/code do agent gửi |
| Safety supervisor | Principal nội bộ, không thể chọn bằng args; chỉ recovery allowlist gắn trạng thái |
| Autopilot/SITL | Tin cậy theo bản pin và cấu hình được ghi lại; không mặc định mọi firmware giống nhau |
| Host administrator/root | Ngoài phạm vi chống compromise. Root có thể sửa code, memory, network và key nếu giữ cùng host |

Không tự nhận MAVLink sysid, UDP address hoặc UUID do một peer tự báo là danh tính mật mã. Với demo: vehicle identity là mapping cấu hình tới một PX4 SITL instance trong mạng cách ly + connection/home generation. MAVLink signing và xác thực sensor vật lý không có trong MVP.

## 4. Trust boundaries

```text
         HUMAN OPERATOR (trusted separate identity)
              | CLI review + Ed25519 signature
              | private key never enters agent/gateway
              v
    signed approval (untrusted transport; verified at gateway)

UNTRUSTED AGENT / MCP CLIENT / PYTHON SANDBOX
              | only authenticated/bound MCP stdio channel
============== B1: client/runtime boundary =================
Trusted launcher -> fixed principal and vehicle binding
MCP handlers + public Drone facade
              |
SafetyGateway: authorization -> policy -> approval verification
              | -> atomic consume + audit intent -> execution lease
============== B2: logical execution boundary ==============
Guarded adapter entry -> scoped/ordered sink dispatch
              |
MAVSDK Python -> private mavsdk_server gRPC sidecar
              |
============== B3: isolated vehicle network ================
PX4 SITL / autonomous flight controller

Ledger + immutable policy + public-key trust store: trusted storage
Telemetry: observed input; must validate, not trust by default
```

B2 nằm trong trusted interpreter; đây là invariant phần mềm, không phải sandbox chống chính code của process. B1+B3 và filesystem permissions tạo G2. Sidecar gRPC 50051 và mọi endpoint phụ cần nằm trong B3, không publish ra agent network/host công cộng.

## 5. STRIDE và đường đối phó

| Nhóm | Kịch bản | Control đề xuất | Bằng chứng cần có |
| --- | --- | --- | --- |
| Spoofing | Giả operator bằng `confirm=true`, tự điền principal/UAV/key | Transport-bound principal, trust store, Ed25519, vehicle/session binding | T01–T03, T07; fake-key/fake-actor tests |
| Tampering | Sửa altitude, yaw, speed, thứ tự mission, policy/home sau duyệt | Strict canonical immutable request, digest, policy/home epochs, final recheck | T06–T11, T21–T22, mutable-list regression |
| Repudiation | Phủ nhận action; client chỉ giữ ACK | Durable request/decision/intent/outcome và token ID hash | Audit completeness, UNKNOWN/crash test |
| Information disclosure | Lộ key/token/credential/position qua log hoặc error | Không log private material, redact, bounded safe error, access policy | Secret-canary tests; key/DB inaccessible từ sandbox |
| Denial of service | Flood approval, mission lớn, lock bị giữ, call treo, DB full | Size/rate/queue limits, bounded I/O, quarantine UNKNOWN, fail-closed mới + recovery riêng | T12,T19,T23; saturation/cancel/disk tests |
| Elevation of privilege | Raw adapter, sidecar gRPC, fake recovery flag, permissive env | Gateway routing, guarded sinks, separate supervisor identity, OS/network isolation | T20a/b/c, T25; direct-port and filesystem probes |

Threat coverage thêm: stale/replayed telemetry (T16), GPS quality (T17), connection interruption (T18), unexpected mode (T24), invalid/mixed policy (T22), concurrent conflicts (T19), monitor death (T23).

Prompt injection trong mission metadata hoặc external telemetry text là input tác động lên agent, không quyền điều khiển gateway. Structured policy không được parse text đó thành instruction. Nếu hiện không có metadata field, test phải dùng trường không được hỗ trợ để xác nhận reject, hoặc harness external-text riêng; không giả vờ một attack surface chưa tồn tại đã bị exploit.

## 6. Attack tree: AI muốn takeoff trái phép

Root: **một takeoff/mission-start không được phép tới backend hoặc làm simulated vehicle bay**.

```text
OR
+-- A. Đi qua API hợp lệ nhưng thiếu thẩm quyền
|   +-- A1. confirm=true                         -> chữ ký độc lập, Boolean không cấp quyền
|   +-- A2. fake actor/operator/public key       -> trust store + transport identity
|   +-- A3. dùng permission chỉ đọc             -> RBAC/allowlist trước approval
|   +-- A4. tự chạy CLI duyệt/đọc key            -> operator key + OS identity separation
|
+-- B. Có approval nhưng dùng sai mục đích
|   +-- B1. replay/several concurrent calls     -> atomic persistent nonce claim
|   +-- B2. expired/chờ lock đến hết hạn        -> wall+monotonic expiry; check tại dispatch
|   +-- B3. đổi action/UAV/altitude/yaw/speed    -> exact canonical binding
|   +-- B4. đổi mission/list sau validation     -> freeze before await; immutable execution plan
|   +-- B5. policy/home/connection đổi          -> version+epoch invalidation
|   +-- B6. khôi phục DB cũ/restart             -> boot epoch mới; không tự resume/reset ledger
|
+-- C. Không dùng takeoff tool
|   +-- C1. fly_mission có implicit takeoff     -> mission cũng cần approval; air-state rules
|   +-- C2. raw Drone.adapter/MavsdkAdapter     -> G1 guarded entry, G2 isolation
|   +-- C3. mavsdk_server gRPC trực tiếp        -> sidecar private network, no exposed port
|   +-- C4. MAVLink peer/serial khác            -> isolated network/devices, trusted peers only
|   +-- C5. khởi động server permissive thứ hai -> không quyền launcher/config/network; singleton
|
+-- D. Dụ bypass bằng lỗi hoặc tình trạng khẩn
|   +-- D1. fake emergency/recovery=true        -> principal nội bộ không client-selectable
|   +-- D2. stale/missing telemetry => assume OK-> deny new action; explicit degraded behavior
|   +-- D3. timeout/cancel rồi auto retry       -> consumed token + UNKNOWN, no automatic retry
|   +-- D4. monitor/audit/DB chết               -> inhibit new action; protected bounded recovery
|   +-- D5. malformed config fallback          -> strict startup policy, no silent downgrade
|
+-- E. Chiếm trusted computing base
    +-- sửa server memory/code, root, stolen operator key, compromised autopilot
        -> ngoài bảo đảm MVP; phát hiện/giảm thiểu vận hành, không claim được chặn
```

Đây là tree các đường liên quan implementation/scope đã biết, không tuyên bố liệt kê được mọi lỗ hổng có thể có của toàn hệ sinh thái.

## 7. Safety khác security như thế nào

- Từ chối lệnh mới không dừng mission đã được autopilot nhận. Monitor cần quan sát ongoing flight, nhưng process chết vẫn có thể để UAV bay tiếp.
- HOLD/RTL/LAND không an toàn trong mọi hoàn cảnh: GPS, home, địa hình, battery, mode và PX4 configuration quyết định tính phù hợp.
- Readonly là restriction cho agent; không cho mọi client vượt readonly chỉ vì gọi tên “recovery”. Supervisor có quyền giới hạn, policy và audit riêng.
- Không disarm airborne hoặc state unknown. Không dùng việc giết process để mô tả “đưa về trạng thái an toàn”.
- Emergency policy dựa trên profile PX4 đã kiểm thử. Mất link: inhibit new commands, ghi uncertainty, dựa trên failsafe đã cấu hình của autopilot; không báo đã HOLD/LAND nếu không có đường gửi/quan sát.
- Nếu có partial write (upload/set-alt) trước expiry/error, ghi rõ partial; không tính case đó là “không có gì tới adapter”.

## 8. Security invariants phải thành test

| ID | Invariant |
| --- | --- |
| I01 | DENY/REQUIRE_APPROVAL trước dispatch tạo zero physical write; telemetry reads được phép ghi riêng |
| I02 | Mọi write sink đều có execution lease hợp lệ, bind operation+params+order+count |
| I03 | Caller không thể tự chọn operator/principal/supervisor authority |
| I04 | Bytes được human xem/ký và immutable plan được thực thi tương ứng cùng canonical digest |
| I05 | Token có tối đa một gateway dispatch attempt; retry/restart/concurrency không phục hồi quyền đã consume |
| I06 | Không có exactly-once vehicle guarantee; ACK, gateway attempt và actual vehicle outcome là ba số đo khác nhau |
| I07 | Reconnect/home/policy/boot epoch đổi hủy approval/lease cũ |
| I08 | UNKNOWN/timeout/cancel không tự retry; late task không được phát sink tiếp theo |
| I09 | Bad/missing/stale telemetry không tự được nâng thành healthy |
| I10 | Không có bất kỳ gateway physical write nếu ledger/audit intent không durable; supervisor recovery cũng không được miễn. New dangerous action còn cần monitor health hợp lệ |
| I11 | Recovery không được cấp quyền bằng text/Boolean từ client; airborne disarm luôn bị chặn ở gateway |
| I12 | G2 chỉ được claim sau sandbox tests của mạng, gRPC, serial, filesystem và launch authority |
| I13 | Denial của LLM hoặc autopilot không được tính là gateway prevented write |
| I14 | DB rollback/crash không thể biến approval cũ thành mới: boot epoch đổi và startup fail-closed |

## 8b. Kết quả thử bypass đã chạy thật (G1 đạt, G2 chưa)

Năm đường vòng được thử trên code hiện tại, mỗi lần kiểm tra xem sink vật lý
có chạy không. Kết quả thật, không phải suy luận:

| Đường vòng | Kết quả | Ghi nhận |
| --- | --- | --- |
| `Drone` thứ hai trên cùng adapter, không gateway | chặn | `READONLY` |
| Gọi `adapter.arm()` trực tiếp | chặn | `LeaseError` |
| Tự dựng `ExecutionLease` rồi truyền vào adapter | chặn | `LeaseError` |
| Gắn gateway thứ hai vào adapter đã bind | chặn | `LeaseError` |
| Đọc `gateway._issuer` rồi dựng lease hợp lệ | **không chặn** | `arm` thật sự chạy |

Đường thứ năm là ranh giới G1/G2: cơ chế lease chặn bypass qua public API và
qua agent, nhưng **không** chặn code đã chạy được Python trong cùng
interpreter. Không được mô tả hệ thống là “mọi lệnh bắt buộc qua gateway” mà
không kèm điều kiện này. G2 cần sandbox OS-level và vẫn chưa được kiểm thử.

## 9. Residual risks và giới hạn nghiên cứu

1. Chữ ký chứng minh quyền dùng key, không chứng minh người hiểu hậu quả hoặc chống được ép buộc/social engineering.
2. Receipt freshness không xác thực vật lý của sensor; GPS spoofing và autopilot độc hại nằm ngoài benchmark chính.
3. Hash-chain audit không chống root/DB writer viết lại cả history nếu không có checkpoint độc lập; không gọi tamper-proof.
4. Fencing chỉ target và policy runtime không bảo đảm đường bay liên tục trong mọi dynamics; mô hình flat simulation không có terrain/collision.
5. Event-loop monitor không độc lập trước event-loop freeze; autopilot failsafes/OS supervisor bổ trợ, không claim hard real-time.
6. Single vehicle, single trusted gateway owner; fleet, multi-process distributed authorization, key recovery enterprise và hardware certification ngoài scope.
7. Dependency pin/scan giảm uncertainty chứ không chứng minh không có lỗ hổng.

## 10. Gate phê duyệt thiết kế

Trước implementation, chủ dự án cần duyệt kiến trúc với ba điều kiện được nêu rõ: breaking change `confirm=true`; operator-key separation thực tế; và scope G1/G2. Mô hình triển khai không được âm thầm bỏ G2 rồi tuyên bố đã đạt yêu cầu “direct Python không bypass”.

Đề xuất triển khai chi tiết và test T01–T25 được chốt trong [ARCHITECTURE.md](ARCHITECTURE.md) và [SAFEMAV_IMPLEMENTATION_PLAN.md](SAFEMAV_IMPLEMENTATION_PLAN.md). Lượt audit hiện tại **không phải sự phê duyệt chạy code/deployment**.
