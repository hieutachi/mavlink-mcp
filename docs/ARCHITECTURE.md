# SafeMAV — Kiến trúc đích và hợp đồng triển khai

Ngày: 05/11/2026. **PROPOSED — chưa implementation, chưa được chủ dự án phê duyệt triển khai.**

Tài liệu này chủ động chốt một phương án nhỏ để mô hình thực thi không phải tự thiết kế lại. Mọi tên class/tool/CLI mới bên dưới là **API dự kiến**, chưa tồn tại. Đọc [audit](INITIAL_SECURITY_AUDIT.md) và [threat model](THREAT_MODEL.md) trước.

## 1. Quyết định kiến trúc

| ADR | Quyết định đề xuất | Lý do/giới hạn |
| --- | --- | --- |
| A01 | Giữ package `mavlink_mcp`, console `mavlink-mcp`, 12 tên tool hiện tại | Không rebrand package; SafeMAV là tên sản phẩm/competition |
| A02 | Một `SafetyGateway` cho cả Drone facade và MCP; một writer cho một vehicle | Tránh hai policy path hoặc framework fleet |
| A03 | Ed25519 từ `cryptography`, operator CLI ngoài agent, public-key trust store | Không tự tạo crypto; gateway không giữ private signing key |
| A04 | Strict immutable normalized request trước await; số có đơn vị chuyển thành integer | Giải quyết mutable mission và canonical binding rõ ràng |
| A05 | SQLite stdlib cho request/nonce/event trong cùng durable transaction | Không Redis/distributed ledger; không giả atomicity với autopilot |
| A06 | Scoped execution lease kiểm tra trước cả 10 physical write sinks | Không chỉ bảo vệ 8 tool entrypoints |
| A07 | Unknown/timeout/cancel => consume giữ nguyên, quarantine, không retry tự động | At-most-one gateway attempt, không claim exactly-once delivery |
| A08 | Snapshot telemetry có per-field receive times, liveness/epoch/home anchor | Không gọi một timestamp ở cuối gather là freshness |
| A09 | Operator và internal supervisor là principal khác agent; recovery qua cùng gateway | Không bypass bằng `emergency=true`; không airborne disarm |
| A10 | G1 API và G2 deployment tách gate; G2 demo trên Linux/WSL2 isolation | Same-process Python không phải adversarial sandbox |
| A11 | Raw benchmark, audit, failure traces là core; dashboard để sau | Trọng tâm safety 35% + robustness 25% |

## 2. Luồng và module

```text
Trusted launcher binds principal + environment + vehicle + protected policy
              |
 MCP handlers / Python Drone facade
              |
   strict normalizer -> FrozenRequest
              |
     SafetyGateway.submit(request, trusted_context, approval?)
        | authorization / deterministic policy
        | request store / approval verifier / one-use ledger
        | per-vehicle arbitration / final freshness checks
        v
  ExecutionLease -> guarded backend -> MAVSDK sidecar -> PX4 SITL
        ^                  |
        |                  v
  Supervisor <--- telemetry subscribers / execution reconciliation
        |
  typed recovery request through the SAME gateway, never raw adapter

  Human approval CLI: read once -> display exact snapshot -> user confirms
                    -> Ed25519 sign -> token returned over untrusted channel
```

### 2.1 Tệp dự kiến

| Tệp | Trách nhiệm duy nhất |
| --- | --- |
| `safety/models.py` | Frozen request, policy/result enums, context, snapshot, execution plan/lease data; normalization/canonical bytes |
| `safety/config.py` | Strict protected config, profile/role allowlists, version/hash; atomic load, no silent fallback |
| `safety/authorization.py` | Permission principal/operator/vehicle/action, trusted context resolution contract; không đọc actor từ args |
| `safety/approval.py` | Ed25519 sign/verify payload, trust-store lookup, TTL/binding; không tự gửi lệnh |
| `safety/store.py` | SQLite requests/nonces/audit events, transactional claim, query/restart reconciliation |
| `safety/policy.py` | Pure deterministic evaluation và risk labels; không I/O hoặc gọi LLM |
| `safety/state.py` | Gateway/request state transitions, arbitration, recovery eligibility |
| `safety/gateway.py` | Orchestration, locks, expiry/recheck, lease issuance, dispatch and UNKNOWN handling |
| `safety/monitor.py` | Snapshot aggregation, health/watchdog và observation/recovery requests |
| `operator_cli.py` | Keygen/review/sign/verify CLI cho người thật; never an MCP tool |
| `client.py` | Python client surface qua MCP cho untrusted scripts trong G2; không raw adapter |

`audit.py` và `risk_assessor.py` chưa cần tách: audit ở store, risk bảng trong policy. Không xây plugin system/OAuth server/web dashboard cho bản đầu.

Tệp cũ cần chỉnh: `drone.py`, `adapter.py`, `capabilities.py`, hai adapters, `server.py`, `__init__.py`, `pyproject.toml`, fixtures/tests/examples tương ứng. `guardrails.py` giữ thuật toán/tên cần tương thích nhưng SafeMAV protected mode chỉ nhận policy snapshot, không reread env từng check.

## 3. Hợp đồng dữ liệu

### 3.1 Dữ liệu input và canonicalization v1

Normalizer chung cho MCP và Python, trước await đầu tiên:

- Chỉ nhận schema đã khai báo; reject unknown field, bool-as-number, string số ở API strict, NaN/Inf, dữ liệu ngoài range/precision; không tự làm tròn để được phép.
- Deep-copy list waypoint thành immutable tuple của immutable records; không giữ dict/list của caller.
- Apply mọi default trước khi hash: `takeoff` mặc định 10 m như tool hiện tại, yaw 0, mission speed 5 m/s (mapping mặc định adapter hiện tại).
- Lat/lon: `latitude_e7`, `longitude_e7`, độ WGS84 nhân 10^7; phạm vi ±90/±180 độ. Altitude/speed: integer mm và mm/s; yaw: integer millidegrees, `0 <= yaw < 360000`.
- Dùng `Decimal(str(value))` khi kiểm tra số có biểu diễn chính xác trong độ phân giải đã định. Giá trị dư precision bị reject kèm reason, không silently truncate. Một test di trú riêng cho caller cũ dùng yaw 450 độ.
- Altitude relative phải dương với takeoff/goto/waypoints; speed dương và <= policy.max_speed; không nhận target absolute do agent tự trộn frame.
- Canonical payload chỉ có dict keys ASCII đã định, integers, bounded strings/enums, arrays/tuples và null khi schema cho phép. Không có float trong bytes ký.
- Serialize bằng JSON `sort_keys=True`, `separators=(',', ':')`, `ensure_ascii=True`, `allow_nan=False`, UTF-8; publish golden test vectors. Đây là **restricted canonical format v1 nội bộ**, không tuyên bố tuân RFC 8785 toàn bộ.
- Parser token/review file phải reject duplicate JSON keys; không parse file lại sau khi display. Bounded input trước parse: proposed cap 256 KiB/request; max 1000 waypoints, max 20 pending requests/principal, bounded queue 32. Nếu FastMCP transport không hỗ trợ cap trước parse, ghi rõ lớp nào chỉ áp dụng sau parse và bổ sung cap ở trusted relay cho G2; không bịa transport protection.

Integer normalization là bảo đảm semantic parameters chính xác trong độ phân giải công bố; không phải giữ nguyên biểu diễn text `10` so với `10.0`. Thay đổi có ý nghĩa trong canonical payload luôn đổi digest.

### 3.2 FrozenRequest

Các field bắt buộc của immutable request do gateway tạo:

```text
schema_version = 1
request_id                         server-generated UUID
actor_id                           from trusted transport, not client args
vehicle_id                         protected deployment mapping
gateway_id, boot_epoch             protected instance + random fresh startup epoch
environment                        mock | kinematic | px4_sitl (no hardware profile v1)
connection_epoch, home_epoch        generation ids, stale generation invalidates request
policy_hash                        exact validated policy snapshot digest
action                             arm|disarm|takeoff|land|hold|return_to_launch|goto|fly_mission
parameters                         strict normalized structure, resolved defaults
reference_frame                    WGS84 + HOME_RELATIVE where applicable
home_anchor                        frozen valid lat/lon/absolute altitude when needed
mission_digest                     SHA-256 canonical ordered waypoints, or null
request_digest                     SHA-256 of defined immutable content, excludes itself
created_at_utc_ms
```

Query/retry không được đổi actor, environment hoặc request data. Server có mapping action MCP `goto_location` -> internal `goto`; mapping cố định trước hash.

### 3.2a Home/frame và chuyển đổi protocol

`home_anchor` canonical gồm `latitude_e7`, `longitude_e7`, `absolute_altitude_amsl_mm`, `vertical_datum="AMSL"`, `home_epoch`. WGS84 mô tả tọa độ ngang, không biến AMSL thành ellipsoid height. API MAVSDK 3.17.2 đã cài được kiểm tra: `Position.absolute_altitude_m` và `Action.goto_location.absolute_altitude_m` đều là mét AMSL; relative altitude là so với takeoff altitude. Profile phải xác minh home reference và takeoff reference tương ứng trong PX4 test, không giả định tương đương trên mọi firmware.

Input của agent vẫn phải đúng e7/mm grid, không làm tròn. **Riêng measurement tin cậy** sau kiểm finite/range được quantize `Decimal(str(value))` với ROUND_HALF_EVEN tới e7/mm, ghi raw observation và rounding error trong evidence; không reject mọi sample lệch grid hoặc đưa raw float vào bytes ký. Home epoch tăng khi connection generation đổi, explicit home reset, hoặc bất kỳ canonical home field đổi; duplicate sample giống nhau không tăng epoch. Thay đổi trước dispatch invalidates request, không âm thầm đổi anchor.

Execution plan giữ immutable integer target; goto absolute target = canonical home AMSL mm + relative mm. Adapter chuyển sang số MAVSDK duy nhất bằng phép chia integer với 10^7/1000 và chuyển đổi protobuf theo phiên bản đã pin; không fetch home lần nữa. Kiểm thử phải inspect kiểu wire thực tế, round-trip serialize và đo target hiệu dụng. Policy v1 thêm `numerical_margin_m=0.05`: intended target và effective wire target đều phải nằm trong fence/ceiling đã trừ margin; reject nếu tổng sai số quantization+serialization vượt margin. Margin này chỉ xử lý sai số số học, **không** bảo đảm GPS accuracy/dynamics. Golden vectors phải có off-grid home, số âm, AMSL lớn, gần fence/ceiling, và migration của input đúng tại ceiling cũ. Nếu không xác minh được datum/wire conversion thì deny, không đoán.

### 3.3 Approval payload và token

ApprovalRequest còn có `nonce` (32 bytes ngẫu nhiên, base64url), `issued_at_utc_ms`, `expires_at_utc_ms`, `required_operator_role`. Nonce do gateway phát và lưu với `NonceState=PENDING`; request tương ứng là `RequestState=AWAITING_APPROVAL`, không có RequestState mang tên PENDING. Tạo cả hai record và deadline trong cùng transaction. Thời gian do gateway phát hành là bất biến; operator ký lại nguyên thời gian đó, không cấp TTL mới.

CLI tạo signed claims:

```text
approval_schema_version = 1
key_id, operator_id
request_id, request_digest
actor_id, vehicle_id, gateway_id, boot_epoch, environment
connection_epoch, home_epoch, policy_hash
action, parameters, reference_frame, home_anchor, mission_digest
nonce, issued_at_utc_ms, expires_at_utc_ms
```

`signature = Ed25519.sign(b'SafeMAV-Approval-v1\x00' + canonical_claims)`.

Token gồm payload và signature base64url. Không nhận public key/algorithm do client lựa chọn; algorithm cố định Ed25519, `key_id` chỉ lookup trust store do operator quản lý. Verify đối chiếu request AWAITING_APPROVAL và nonce PENDING, operator role, key active và vehicle/action scope. Key/permission revocation trước một sink tiếp theo cũng làm lease invalid; chữ ký đúng không miễn final authorization check.

Không gọi `hash()` của Python để làm digest. SHA-256/Ed25519 là primitives chuẩn; canonicalization phải có version/golden vectors. Không log raw bearer token hoặc key.

### 3.4 Clocks và expiry

- Proposed approval TTL tối đa 60 giây; policy có thể ngắn hơn. Ký UTC milliseconds; `now >= expires_at` là expired. Giới hạn `expires-issued <= max_ttl`, reject future issue quá tolerance được công bố.
- Trong boot hiện tại, ghi monotonic deadline ngay lúc tạo request; kiểm cả UTC expiry và deadline monotonic. Không reset TTL khi verify lại hoặc đợi lock.
- Phát hiện đồng hồ UTC lùi/bất thường so với monotonic => deny authorization mới, không gia hạn token.
- Recheck sau lock, sau chuẩn bị, trước từng sink; expiry sau upload/set-alt chặn start/takeoff và ghi partial effect.
- Boot epoch mới sau mọi restart làm token cũ invalid, kể cả backup DB cũ được restore. Không phục hồi active token qua restart trong v1.
- Store worker thực hiện expiry sweep theo clock đã inject và trước quota/admission check: request chưa ký vẫn hết hạn, nonce thành EXPIRED, quota pending được giải phóng. Retrieval/resubmission không gia hạn. Startup invalidates pending rows của boot cũ và đối soát quota.
- Approval TTL cấp quyền bắt đầu từng authorized sink, không phải thời lượng flight. TTL hết sau khi action đã được dispatch không tự tạo lệnh land/RTL; ongoing flight theo observation/recovery policy.

### 3.5 Kết quả ổn định

```text
PolicyDecision: ALLOW | DENY | REQUIRE_APPROVAL | SAFE_STATE
ExecutionStatus: NOT_STARTED | ACCEPTED | IN_PROGRESS | COMPLETED | FAILED | UNKNOWN
GatewayHealth: DISCONNECTED | INITIALIZING | READY | DEGRADED | FAULT
RequestState: PROPOSED | AWAITING_APPROVAL | AUTHORIZED | EXECUTING
              | COMPLETED | DENIED | EXPIRED | FAILED | UNKNOWN
```

SAFE_STATE là **yêu cầu chọn response theo recovery policy**, không lời khẳng định xe đã an toàn. Gateway health và request state tách nhau để một request chờ duyệt không khóa telemetry hoặc khiến cả UAV “chờ duyệt”.

Response chuẩn thêm: `schema_version, request_id, decision, reason_code, execution_status, audit_event_id, approval_request?`. Reason codes tối thiểu: `UNAUTHORIZED`, `READONLY`, `APPROVAL_REQUIRED`, `APPROVAL_INVALID`, `APPROVAL_EXPIRED`, `APPROVAL_REPLAY`, `PARAMETER_MISMATCH`, `POLICY_CHANGED`, `VEHICLE_CHANGED`, `TELEMETRY_STALE`, `TELEMETRY_INCOMPLETE`, `UNSAFE_STATE`, `OUTSIDE_GEOFENCE`, `ALTITUDE_LIMIT`, `BUSY`, `AUDIT_UNAVAILABLE`, `MONITOR_UNHEALTHY`, `EXECUTION_UNKNOWN`, `INVALID_ARGUMENT`.

Lỗi compatibility giữ `error` và `guardrail:true` khi là safety rejection. Success response không được dùng `armed:true` để chứng minh đã quan sát armed nếu mới nhận ACK; có migration note rõ cho trường cũ. `get_request_status` chỉ đọc kết quả, không retry action.

## 4. Authorization và human approval thật

### 4.1 Principal

V1 một gateway/vehicle/một bound agent identity. Trusted launcher chọn principal từ cấu hình không cho agent sửa. `client_info.name`, `actor_id` trong JSON hoặc tên model không phải credential.

Direct in-process Drone chỉ là API của trusted application: context được binding bởi host, không cho caller vượt policy bằng constructor. Untrusted Python G2 dùng `client.py` qua cùng MCP channel, không nhận một self-declared internal context.

Read tools có permission quan sát nhưng không cần human approval. Agent chưa có quyền request flight thì **DENY**, không tạo approval để xin vượt role. Agent được request flight nhưng thiếu signature thì **REQUIRE_APPROVAL**.

### 4.2 Role/action matrix v1

| Principal | Observe | Request dangerous action | Sign | Automated recovery |
| --- | --- | --- | --- | --- |
| observer | Có | Không | Không | Không |
| flight_agent | Có | Có trong vehicle/action scope; mọi mutation cần token trong v1 | Không | Không |
| human_operator | Qua authorized export/client | Không có operator-originated ingress trong v1 | Có với key riêng, phê duyệt request của flight_agent | Ký request recovery của agent nếu agent vẫn writable; không bypass readonly |
| safety_supervisor | Có | Không arm/takeoff/goto/mission | Không | Chỉ standing recovery allowlist + runtime eligibility |

Readonly chặn **mọi agent mutation**, kể cả có token. Nó không cấp quyền emergency cho agent. Supervisor có standing authority do operator cấu hình riêng từ startup; thông tin này không thể truyền bằng tool args.

V1 không có operator-control RPC riêng. Chữ ký operator chỉ phê duyệt exact request của actor gốc; không biến submitting agent thành operator hoặc vượt readonly. “Operator-approved recovery” dưới đây nghĩa flight_agent writable gửi recovery request và được operator ký. Nếu agent readonly, recovery trong gateway chỉ có internal supervisor nếu eligible; người vận hành có thể dùng trusted autopilot/GCS/manual control ngoài gateway, được ghi là external control, không claim gateway-enforced. Thêm operator-bound ingress vào cùng singleton là extension riêng sau v1, không spawn gateway thứ hai.

### 4.3 Flow

1. Agent gọi một mutation với exact parameters. Gateway normalize, authorize, kiểm policy/context; nếu hợp lệ nhưng chưa được duyệt, atomically lưu request AWAITING_APPROVAL + nonce PENDING + deadline và trả REQUIRE_APPROVAL.
2. Agent xuất approval request JSON hoặc operator lấy bằng read-only request tool. File trung chuyển không tin cậy.
3. Operator chạy CLI trên identity riêng; CLI đọc một lần, kiểm schema/digest, hiển thị operator target, environment, action, **toàn bộ mission**, units, expiry và short digest. Người nhập lại short request ID để xác nhận; không có default yes.
4. CLI ký đúng object vừa hiển thị, xuất token. Không có MCP tool `approve`, không gọi ký trong server, không để coding agent tự chạy bước ký demo rồi gọi đó là “human approval”. Test fixture có test key phải ghi rõ automated test, không phải người thật.
5. Agent gửi lại mutation với `request_id` và `approval_token`, cùng params. Gateway kiểm binding và request record, final policy recheck, claim một lần rồi dispatch.
6. Operator có thể từ chối hoặc để expiry. Không tự hồi sinh request, không tự gia hạn. Cần request mới nếu đổi home/policy/vehicle/mission.

CLI dự kiến: `safemav-operator keygen`, `review`, `approve`, `verify`. Đây là command **sẽ tạo trong gói P06**, không phải command hiện chạy được. Private key ở operator storage, không trong repository/OneDrive hay agent container. Tạo/cấp quyền file thật cần owner thực hiện hoặc cho phép riêng.

## 5. Gateway transaction và execution leases

### 5.1 Flow chính

```text
submit(raw_args, trusted_context, token):
  validate size/schema; freeze normalized args before first await
  resolve actor/vehicle/environment from trusted context
  reject unauthorized/readonly (audit if available)
  load immutable policy version; get valid telemetry/home context
  evaluate policy (no LLM, unknown never silently healthy)
  create/read immutable request; REQUIRE_APPROVAL if missing valid approval
  acquire bounded vehicle dispatch lock / ensure one active owner
  revalidate identity, policy, epochs, monitor health, telemetry, signature, TTL
  SQLite transaction: claim PENDING nonce + append EXECUTION_INTENT + state EXECUTING
  commit durably; failure => zero physical writes
  create opaque scoped lease for exact execution plan
  dispatch one operation, checking lease/current safety before each physical sink
  persist ACK/failure/UNKNOWN and observed completion separately
  release dispatch lock only once old task cannot issue further sinks
```

Policy/crypto có thể được evaluate trước lock để trả lỗi sớm, nhưng final check bắt buộc trong lock. Không giữ vehicle lock khi chờ người duyệt. `connect` lock riêng. Một mission đang bay không giữ lock vô hạn; active-operation arbitration chặn lệnh navigation xung đột và cho phép **recovery đã được phép** theo transition table.

### 5.2 Durable store

Dùng `sqlite3` với transaction explicit, unique constraint nonce/request ID, `BEGIN IMMEDIATE` cho claim, busy timeout bounded, `synchronous=FULL` cho journal mode đã chọn và kiểm thử. Fsync/blocking DB I/O qua dedicated worker, không làm event loop monitor bị nghẽn ngoài budget.

Tối thiểu 3 bảng: `requests`, `approval_nonces`, `audit_events`. Consume nonce và append EXECUTION_INTENT trong **cùng transaction**. Nonce bị burn trước write đầu tiên, kể cả upload mission/set-alt.

Crash matrix:

| Điểm crash | Khi restart |
| --- | --- |
| Trước commit intent | Không dispatch; request cũ hết hiệu lực do boot epoch mới |
| Sau commit, trước write | Quyền đã mất; không tự retry, reconcile conservative |
| Sau write, trước outcome persist | UNKNOWN; không biết lệnh đã thực thi hay chưa |
| Sau ACK | Chỉ accepted; kiểm flight state trước claim completed |

Storage runtime ở local non-synced protected directory do operator cấu hình. **Không dùng SQLite/key runtime trong thư mục OneDrive của repository này.** Missing/corrupt database trong established deployment => FAULT, không tự tạo “fresh nonce database”. Bootstrap là thao tác operator riêng. Restore backup bắt buộc boot epoch mới, pending invalid và reconciliation; không tiếp tục session từ snapshot VM đã đóng băng memory mà không operator reset.

Single-writer policy cũng cần exclusivity trên vehicle, không chỉ SQLite path. V1 không cho hai gateway owner cùng target; deployment giữ singleton, process lock trên trusted state directory, lỗi lock thì không connect/write. OS isolation mới ngăn process trái phép dùng DB/path khác.

**Hai nhánh authority nhưng một dispatch path:** human-approved agent request claim nonce + durable intent; internal supervisor không có human token, dùng non-serializable host authority và protected standing-policy hash. Supervisor tạo unique internal request, đi `PROPOSED -> AUTHORIZED -> EXECUTING`, ghi durable intent và unique internal dispatch ID trong một transaction, rồi nhận lease cùng rule/sink checks. Chỉ recovery allowlist được dùng nhánh này; không arm/takeoff/goto/mission. Duplicate internal trigger được coalesce theo active recovery generation, không spam cùng sink.

V1 **không có durability exception**: ledger/audit-intent unavailable thì mọi gateway physical write, kể cả supervisor recovery, đều bị cấm. Alert/observe nếu còn hoạt động và dựa vào autopilot failsafe đã cấu hình; không báo đã đưa xe an toàn. Audit outcome persist lỗi sau write vẫn UNKNOWN/quarantine. Audit intent có trước write và eventual outcome completeness là hai thuộc tính khác nhau.

### 5.3 Lease tại backend

`DroneAdapter` mutators trong protected runtime nhận keyword-only execution lease, mặc định thiếu => structured safety error. Lease do gateway issue, không nhận qua MCP/JSON, gắn gateway instance, request, immutable execution plan, expiry, epochs và sink budget. Không coi opaque object là bảo vệ trước arbitrary trusted-process code.

Allowed sink sequence:

| Action | Sink budget |
| --- | --- |
| arm/disarm/land/hold/RTL/goto | Đúng một sink tương ứng, đúng params |
| takeoff | `set_takeoff_altitude` rồi `takeoff`, mỗi sink một lần |
| fly_mission | `upload_mission` rồi `start_mission`, mỗi sink một lần |

Guard trước mỗi sink: lease chưa revoked, đúng thứ tự/params, deadline/epoch/policy còn hợp lệ, latest required health valid. Adapter chuyển đổi protocol; guard gọi trusted execution authorizer, không duplicate policy rules trong adapter.

`goto` nhận **resolved absolute target** từ plan được build bằng cùng home anchor đã kiểm tra. Không gọi lại get_home trong adapter rồi tính altitude khác. Mission items được build từ frozen tuple/defaults và kiểm conformance với digest.

MAVSDK có thể retransmit nội bộ. Cam kết chỉ là at-most-one **gateway-level dispatch attempt** cho token, không exactly-once physical execution.

### 5.4 Timeout/cancellation

Call deadline proposed 5 giây, tune theo SITL evidence. Bounded timeout không chứng minh backend dừng. Nếu task phớt lờ cancel hoặc ACK muộn: revoke lease ngay, cấm sink tiếp theo, mark vehicle quarantine/UNKNOWN. Không nhả trạng thái để request khác đua với task cũ; observation/reconciliation vẫn được phép.

`CancelledError` phải có cleanup shield tối thiểu cho durable outcome/lease revocation; không shield cả chuyến bay hoặc auto-finish mission. Disconnect của client không trở thành cancel/rollback vật lý. Không gửi bù một action “ngược lại” tự động.

Sau partial upload/set-alt, TTL expired chặn start/takeoff, ghi `partial_effects=true`. Benchmark không đếm đây là zero-write prevention. Không tự xóa mission backend như “rollback” vì đó cũng là command cần quyền.

## 6. Policy và telemetry

### 6.1 Proposed test profile, không phải tiêu chuẩn bay thật

| Giá trị đề xuất cho controlled PX4 SITL | Giá trị |
| --- | --- |
| max altitude / radius | 50 m / 200 m |
| max waypoint speed | 5 m/s |
| min battery for starting new flight/navigation | 30% theo MAVSDK 3.17.2, range 0–100 |
| GPS requirement | profile-specific 3D fix allowlist + >=10 satellites |
| max critical-field receive age / skew | 1.0 s / 0.5 s |
| max approval TTL / dispatch timeout | 60 s / 5 s |
| payload / pending per actor / queue | 256 KiB / 20 / 32 |

Đây là tham số ban đầu để kiểm thử, **không có bằng chứng là thresholds an toàn cho mọi UAV**. Nếu đo SITL cho thấy không phù hợp: ghi deviation, sửa policy version trước run tiếp, không điều chỉnh âm thầm để bỏ rejection.

`readonly=true` vẫn default. Missing policy/key/ledger/actor không tự chuyển legacy writable. Không cho geofence/altitude=0 trong protected profile; legacy parser cũ không quyết định authority mới.

### 6.2 Snapshot thực tế

MAVSDK backend cần subscriptions dài hạn cho connection, position, battery, GPS, armed, in_air, mode, home; mỗi sample ghi monotonic receipt time và source time nếu API thật cung cấp. Liveness và bounded coherent snapshot dựng từ cache này.

Các field chưa từng nhận, stale, invalid number, khác connection epoch hoặc skew quá lớn => policy không được dùng. Khởi tạo monitor chưa đủ data => INITIALIZING. Heartbeat chỉ báo monitor task chạy, không tự chứng minh sensor update.

Không reset tuổi tất cả field khi gọi `get_telemetry`. Không gọi receive-freshness là source-freshness/anti-spoof. Pin API semantics được xác minh tại version thật, không đoán enum/range của MAVSDK 2.x.

SimAdapter vẫn giữ battery/GPS null. Test policy bằng Mock/Synthetic fixtures có `source=synthetic`; kinematic demo có profile rõ cho sensor unmodelled và environment bị bind trong approval. **Không dùng profile synthetic cho MAVSDK/PX4** hoặc fake GPS/pin lên telemetry hiện tại để làm test xanh.

### 6.3 Action preconditions

| Action | Điều kiện tối thiểu ngoài permission/approval |
| --- | --- |
| arm | connected+fresh, grounded xác định, chưa armed; GPS/pin/mode phù hợp PX4 profile |
| takeoff | armed, grounded xác định, không takeoff đang tiến hành; altitude/fence/current position/health hợp lệ |
| goto/mission | airborne+armed; home/position/GPS/battery/mode fresh; toàn bộ immutable targets trong fence/ceiling; speed hợp lệ |
| disarm | grounded được quan sát chắc chắn, không executing takeoff, không state unknown |
| hold | airborne, supported mode, position/GPS đủ cho holding; không kết luận completed chỉ từ ACK |
| land | operator-approved hoặc standing recovery cụ thể đã test; không mặc định đất bên dưới an toàn |
| RTL | fresh valid home/GPS; PX4 RTL altitude/path/failsafe config được ghi nhận; không chỉ kiểm destination home |

Không cần GPS/pin rule của takeoff để cho phép mọi recovery; từng action có required field riêng. Nhưng thiếu field không tự được “waive” vì tool được gọi là emergency.

Risk labels: READ=LOW; routine waypoint theo policy có thể MEDIUM nhưng v1 vẫn approval; arm/takeoff/mission=HIGH; disarm airborne=CRITICAL/DENY. Recovery risk phụ thuộc context. Không dùng score số giả như xác suất tai nạn.

## 7. State transitions và recovery

### 7.1 Request transitions hợp lệ

```text
PROPOSED -> DENIED | AWAITING_APPROVAL
AWAITING_APPROVAL -> EXPIRED | DENIED | AUTHORIZED
AUTHORIZED -> EXPIRED | DENIED | EXECUTING
EXECUTING -> COMPLETED | FAILED | UNKNOWN
```

AUTHORIZED chỉ là trạng thái ngắn trong request path, không là global flag “mọi hành động đều được phép”. Internal supervisor có riêng `PROPOSED -> AUTHORIZED` khi standing policy hợp lệ; không áp dụng shortcut cho agent. COMPLETED/FAILED/DENIED/EXPIRED/UNKNOWN không chuyển ngược thành EXECUTING; retry cần request mới sau reconcile.

Nonce machine: `PENDING -> CONSUMED | EXPIRED | INVALIDATED`; terminal không quay về PENDING. Request expiry/denial và nonce expiry/invalidation cập nhật atomically; consume nonce + request EXECUTING + audit intent atomically. AUTHORIZED là trạng thái transient có thể được event-log trong final transaction; không để nó tồn tại như quyền dùng lại. Known partial write rồi expiry/denial substep => FAILED + partial_effects; uncertain delivery/late ACK/timeout => UNKNOWN. Sau write không được dùng EXPIRED như thể chưa có tác động.

Gateway: DISCONNECTED -> INITIALIZING -> READY; stale/link lost/health failure -> DEGRADED hoặc DISCONNECTED; storage/owner invariant/cancellation unresolved -> FAULT/UNKNOWN quarantine; recover READY chỉ sau explicit checks, không timer tự reset.

### 7.1a Active-operation admission và completion

Ngoài policy/approval, dispatcher áp dụng bảng dưới đây. “Admit” vẫn cần mọi precondition/action permission; không là ALLOW vô điều kiện. Các read/status calls không chiếm vehicle writer lock.

| Operation đang active | Request mới được xét | Request bị BUSY/UNSAFE_STATE |
| --- | --- | --- |
| Không active, stable grounded/HOLD | Mọi action phù hợp preconditions | Action sai grounded/airborne/mode |
| ARM/DISARM chưa observed complete | Observe/status | Mọi mutation mới |
| TAKEOFF/GOTO/MISSION còn active nhưng không RPC outstanding | HOLD/LAND/RTL recovery nếu authority+profile eligible | ARM/DISARM/TAKEOFF/GOTO/MISSION mới |
| LAND/RTL đang active | Observe/status | Mọi mutation agent/supervisor trong v1; manual autopilot control là ngoài gateway |
| RPC còn outstanding, task không chịu cancel, UNKNOWN quarantine | Observe/status/reconciliation only | Mọi mutation, kể cả recovery |

Recovery được admit tạo request mới có audit link tới active operation cũ. Chỉ sau khi observed recovery state xác nhận thay thế mới đánh dấu request cũ FAILED với reason `SUPERSEDED_BY_RECOVERY`, không COMPLETED; recovery chưa dispatch/bị deny thì giữ request cũ active, delivery không rõ thì đánh dấu uncertainty tương ứng. Không tự cho resume mission.

Completion phải dựa trên ít nhất ba fresh samples trải qua >=1 giây, đúng epoch và bounds; threshold dưới đây là demo profile để kiểm thử, không tiêu chuẩn hardware:

| Action | Observed completion predicate | Completion observation deadline đề xuất |
| --- | --- | --- |
| arm/disarm | armed true/false tương ứng, grounded xác định | 10 s |
| takeoff | airborne+armed, altitude trong 0.5 m của target, stable expected HOLD và trong limits | 120 s |
| goto | distance tới target <=2 m, altitude error <=0.5 m, expected HOLD | 180 s |
| mission | pinned mission progress/finished observation xác nhận hoàn tất, expected post-mission mode và valid position | 600 s |
| hold | expected HOLD + bounded position drift theo profile | 15 s |
| land/RTL | in_air=false, grounded; armed=false nếu profile cấu hình auto-disarm | 180 s / 300 s |

P09 phải inspect actual pinned Mission progress/finished API trước tích hợp; nếu thiếu observation không được thay bằng “mode rời MISSION nên đã xong”. RPC timeout 5 giây và observation deadline flight là **hai đồng hồ khác nhau**. Completion timeout => UNKNOWN (có thể UAV vẫn đang làm), không tự resend hoặc land. Known backend rejection trước delivery => FAILED; unknown delivery luôn UNKNOWN.

Quarantine không tự reset theo timer. Điều kiện mở lại: không còn live dispatch task/lease có thể ghi; mọi outstanding substep đã được quan sát/đối soát hoặc process đã được trusted operator restart sau kiểm tra vehicle; fresh coherent samples đúng epoch cho stable grounded/HOLD; ledger/monitor/policy healthy. Request UNKNOWN cũ giữ kết quả lịch sử, không được redispatch. V1 không có agent/operator RPC để ép unlock; trusted owner thực hiện restart/reconciliation, với boot epoch mới. Restart gateway **không** dừng UAV: nếu cần can thiệp flight, operator dùng trusted autopilot/manual control ngoài gateway trước và ghi sự kiện external intervention.

### 7.2 Recovery matrix

| Điều kiện | Hành vi v1 |
| --- | --- |
| Grounded + request bị deny | Không phát lệnh, ghi event; không cần “emergency disarm” |
| Airborne + violation + connected + GPS/position đủ | Supervisor có thể request HOLD nếu standing policy cho phép và PX4 profile đã test; quan sát outcome |
| Airborne + GPS invalid | Không tự giả định HOLD/RTL phù hợp; inhibit new navigation, operator alert; dựa vào autopilot failsafe đã cấu hình |
| Link lost | Không gửi lệnh qua link không có; state DEGRADED/DISCONNECTED; theo dõi reconnect, cấm reuse lease |
| Battery emergency | Không dùng universal auto-land; notify/deny new mission, apply verified PX4 failsafe; operator signed recovery nếu đủ context |
| Airborne/unknown + disarm | DENY |
| Outstanding command outcome UNKNOWN | Quarantine; không tự overlap một recovery khác với task cũ; operator reconciliation/manual autopilot control |
| Agent readonly | Agent mutations DENY; không phá readonly; supervisor authority riêng vẫn theo policy |
| Ledger/audit unavailable | **Mọi gateway physical write DENY**, kể cả recovery; alert/observe nếu có thể, dựa vào verified autopilot failsafe; không claim đã recovery hoặc durable completeness |

Supervisor không bypass gateway. Recovery request có internal authority object host tạo, bounded allowlist và log. Default auto-recovery allowlist trống đến khi SITL test profile xác nhận; có thể bật HOLD riêng. Land/RTL tự động không nằm trong default v1.

Monitor là task tách trách nhiệm khỏi agent, không phải hard real-time independent safety controller. Event-loop chết có thể làm cả gateway/monitor chết; cấu hình autopilot failsafe là lớp độc lập cuối cùng trong scope SITL. External supervisor phát hiện process death không đồng nghĩa có thể đưa UAV về safe state.

## 8. Audit design

Mỗi event: schema/event sequence/request ID/UTC time/actor/action/normalized parameters hoặc digest/decision/reason/operator key ID/approval nonce hash/execution status/sink sequence/epochs/previous event hash/current hash. Không raw token/private key, không metadata text không cần thiết.

Single writer hash-chain trong SQLite; export JSONL để tái lập. Verifier phát hiện content/reorder/deletion nội bộ. Để phát hiện tail deletion/history rewrite cần checkpoint signed/digest lưu ngoài quyền DB writer; nếu chưa làm, công bố giới hạn đó.

Denied input trước khi parse không có request ID thì tạo event ID riêng và bounded input fingerprint, không log payload độc hại vô hạn. Audit completeness tính cả DENY/REQUIRE_APPROVAL/UNKNOWN, không chỉ success. Emergency degraded event volatile phải được đánh dấu chưa durable; không điền giả đầy đủ.

## 9. Protected deployment G2

### 9.1 Topology đề xuất cho WSL2/Linux demo

- Trusted host harness/operator khởi động PX4 SITL và gateway trong private vehicle network. Sidecar gRPC chỉ ở runtime/private network, không publish 50051 hay MAVLink ports ra agent.
- Agent/Python test container `network=none`, không serial/USB/device, không Docker socket, không host PID/mount, không mount policy/DB/operator key. Có thể có thư viện MAVSDK để test bypass, nhưng không có đường tới target.
- Trusted harness chỉ relay MCP stdio giữa client và gateway. Không dùng agent-controlled shell command để chọn executable/config; stdout thuần protocol, stderr riêng, byte/frame bounds và backpressure.
- Operator CLI ngoài agent container, private key chỉ operator identity giữ. Gateway mount public trust store read-only, không private key.
- Trusted gateway code/config mount immutable; runtime SQLite local non-synced, chỉ gateway identity ghi. One vehicle owner instance. Identity của MCP channel là fixed principal do launcher bind.

Không expose một HTTP approval endpoint hoặc cấp Docker control cho agent cho tiện demo. Không yêu cầu đổi cấu hình Qoder/MCP đang dùng trong lượt kế hoạch này.

### 9.2 Gate để claim G2

Agent sandbox không thể: đọc key; sửa policy/trust store/DB; spoof supervisor; đổi env của gateway; gọi gRPC 50051; gửi UAV UDP/TCP; mở serial; spawn owner service thứ hai; dùng raw Python API đạt physical sink. Positive control qua gateway với token hợp lệ vẫn hoạt động.

Chỉ “connection refused” tới port sai không đủ: harness phải chứng minh target thật đang hoạt động và trusted gateway truy cập được cùng service. Record identity/network config/image digests, không chỉ screenshot container tên “safe”.

Windows native demo cùng tài khoản với coding agent có thể chỉ minh họa G1. Không claim G2 nếu operator key nằm trong filesystem agent đọc được. G2 chưa đạt là blocker cho master-prompt gate direct arbitrary Python, không phải hạng mục tùy chọn có thể đánh dấu done.

## 10. Compatibility và test migration

- `Drone(adapter, guardrails=...)` còn dùng read surface; write không có gateway/trusted binding/approval trả lỗi an toàn. Không auto-build permissive gateway.
- `Drone.adapter` chuyển sang descriptor hoặc deprecate rồi remove trong protected release; code server dùng `backend_info()`/telemetry service.
- 12 existing tool names giữ; mutation thêm `request_id`/`approval_token`. `confirm` có thể giữ deprecated để trả message rõ nhưng không có quyền. Read-only tools bổ sung `get_approval_request` và `get_request_status`, chỉ đọc request cùng actor hoặc authorized operator export, không tạo IDOR; cập nhật test đếm tool từ 12 sang contract mới, không khẳng định vẫn đúng 12 khi đã thêm.
- MCP schema phải giữ strict numeric types ngay **trước coercion của FastMCP** (ví dụ strict Pydantic types nếu phiên bản pin hỗ trợ, khai báo direct dependency khi dùng). Không dùng signature `float` rồi hy vọng core phân biệt được `true` đã bị ép thành1. Contract test raw MCP bool/string/extra fields là bắt buộc.
- Các success assertions cũ cần migration sang ACK/observed outcome. Ghi rõ breaking behavior, không cố giữ hình thức tương thích bằng unsafe escape hatch.
- `tests/conftest.py` thêm trusted-test context, deterministic clock, ephemeral test operator key và sink spy; không dùng real endpoint. Existing readonly/geofence/input/kinematic regressions tiếp tục tồn tại.

## 11. Không làm trong v1

Không rename repository/package, không OAuth platform, không React dashboard, không đa UAV, không ROS adapter mới, không ArduPilot/hardware validation, không autonomous key approval, không LLM risk judge, không tự phát hành/push/submit, không benchmark giả. Chỉ bắt đầu implementation sau approval theo [kế hoạch gói việc](SAFEMAV_IMPLEMENTATION_PLAN.md).
