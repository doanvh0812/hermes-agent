# Zalo Transfer Receipt → Odoo Payment

**Status:** draft — design only, nothing implemented

**Author:** doanvh0812

**Consumers:** `plugins/platforms/zalo`, `vni_edu/vni_invoice`

## Scope

Người dùng gửi ảnh chụp biên lai chuyển khoản qua Zalo. Hệ thống đọc ảnh,
trích số liệu, tìm hoá đơn khớp, **gửi lại cho người dùng xác nhận**, và chỉ
ghi nhận sau khi được đồng ý.

Nguyên tắc xuyên suốt: không có dữ liệu nào được tạo trước bước xác nhận.

Tài liệu này chốt **ranh giới tin cậy** và **hợp đồng ghi**. Phần OCR và
endpoint là follow-up, không nằm trong slice này.

Không thuộc phạm vi: đối soát sao kê ngân hàng tự động, phát hiện ảnh giả
mạo, thanh toán một lần cho nhiều hoá đơn.

## Vì sao agent không được tự ghi

Deployment Zalo hiện tại read-only ở bốn lớp độc lập:

| Lớp | Vị trí | Nội dung |
|---|---|---|
| SOUL floor | `plugins/platforms/zalo/deploy/SOUL.snippet.md` | cấm đổi state kể cả khi skill không load |
| Skill | `deploy/skills/odoo/odoo-chat-support/SKILL.md` | `pay`, `post`, `reconcile` nằm trong danh sách cấm |
| MCP allowlist | `deploy/profile/config.yaml` | 7 tool đọc; `ODOO_MCP_ENABLE_WRITES` bỏ trống có chủ đích |
| Tool surface | `platform_toolsets.zalo` | terminal tắt — agent có terminal là đi thẳng XML-RPC, vượt mọi hạn chế MCP |

Bật `ODOO_MCP_ENABLE_WRITES` sẽ mở quyền ghi cho **toàn bộ** bề mặt MCP, không
riêng payment — và làm rỗng nghĩa của ba lớp còn lại. Credential Odoo mà bot
dùng là tài khoản admin (xem chú thích đầu `deploy/odoo-mcp/field_policy.json`),
nên record rule của Odoo không phải hàng rào ở đây; `field_policy.json` mới là.

Vì vậy: **agent đọc và đề xuất, không ghi.** Việc ghi đi qua một endpoint
riêng, xác thực riêng, phạm vi hẹp — nêu ở phần "Hợp đồng ghi".

## Luồng

```
Zalo: user gửi ảnh
  └─ adapter lưu file + ghi index          [đã có]
       └─ OCR ảnh → {amount, date, ref, memo, bank}   [thiếu]
            └─ agent tìm account.move khớp  [đã có: 7 tool đọc]
                 └─ agent gửi lại số liệu, hỏi xác nhận  [thiếu]
                      └─ user xác nhận trong Zalo         [thiếu]
                           └─ endpoint ghi → biên lai chờ [thiếu]
                                └─ payment               [xem "Chuyển thành payment"]
```

Không có bước nào ghi trước khi người gửi xác nhận. Ảnh vào chỉ nằm ở dạng
đề xuất trong hội thoại; số liệu chỉ rời khỏi vùng nháp khi người gửi đọc lại
và đồng ý.

### Bước xác nhận

Agent gửi lại đúng năm trường đã trích — số tiền, ngày, mã giao dịch, nội
dung, và hoá đơn được đề xuất — rồi dừng. Không ghi gì cho tới khi có trả lời
đồng ý.

Quy tắc trình bày theo `deploy/odoo-mcp/instructions.txt`: tiền `1.234.567 đ`,
ngày `dd/mm/yyyy`, không Markdown, không nhắc tên model/field.

Trả lời không rõ ràng thì hỏi lại, không suy diễn thành đồng ý. Xác nhận phải
đến từ **chính người đã gửi ảnh**, trong cùng luồng hội thoại — `sender` và
`thread` đã có sẵn trong record của attachment index, dùng lại để so.

Xác nhận hết hạn sau một khoảng ngắn. Một biên lai treo nhiều ngày rồi mới
được "đồng ý" gần như chắc chắn là người dùng trả lời nhầm tin nhắn cũ.

### Mắt xích 1 — đọc ảnh

**Không cần viết OCR, và không sửa `adapter.py`.** Ảnh đã đến được agent:
`classify_inbound` đưa URL ảnh lên `media_urls` (`adapter.py:406`, dùng tại
`:1916`) và vision tool fetch thẳng từ đó — xem docstring module
`adapter.py:37`. Toolset `vision` đã bật sẵn trong `platform_toolsets.zalo`.

`_extract_text()` (`adapter.py:1137`) trả `None` cho ảnh, nhưng **đó không
phải chỗ cần sửa**: nó là hàm đồng bộ chạy lúc nhận tin nhắn, trước khi agent
khởi động, trong khi `vision_analyze` là tool chạy trong vòng lặp agent
(`toolsets.py:136`). Adapter không gọi được tool của agent. Thêm OCR ở tầng
adapter nghĩa là kéo thêm dependency (tesseract) cho việc vision tool đã làm
được.

Vì vậy việc trích số liệu thuộc về **skill**, không thuộc về code adapter:
skill mô tả cần đọc những trường nào, agent gọi `vision_analyze`.

Trích tối thiểu:

| Trường | Ghi chú |
|---|---|
| `amount` | bắt buộc |
| `transaction_ref` | bắt buộc — khoá idempotency |
| `transfer_date` | bắt buộc |
| `memo` | nội dung CK; mã lớp thường nằm ở đây |
| `bank` | tuỳ chọn |

Mã lớp có thể đến từ `memo` hoặc từ chính tin nhắn người dùng gõ kèm ảnh —
cả hai đều chấp nhận, xem "Mắt xích 2".

### Mắt xích 2 — khớp hoá đơn

Khớp bằng số tiền là không đủ, và đây là chỗ dễ sai nhất trong cả luồng.

**Vì sao mã lớp không định danh được một hoá đơn.** `pti.class.out_invoice_ids`
là One2many (`models/pti_class.py:16`) — một lớp có nhiều hoá đơn. Nhân lên
ba chiều nữa: ba loại người trả (`student`/`school`/`partner`), trả góp nhiều
đợt (`number_of_payment`, chặn tại `wizard/vni_class_invoice.py:224`), và
`_create_class_move` gộp theo lớp + ngày (`:29`) nên mỗi đợt sinh một
`vni.class.move` riêng. Một lớp 20 học viên × 3 đợt đã là 60 hoá đơn.

Mã đơn hàng (`ref`) khá hơn nhưng vẫn không đủ: `create_out_invoice` set cùng
`ref = order_id.name` cho **mọi** đợt của một học viên (dòng 237, 256, 275),
nên trả góp làm nhiều hoá đơn dùng chung một `ref`.

**Cách thu hẹp.** Người nộp gửi **mã lớp** kèm ảnh; bot hỏi thêm **loại thu
tiền**, map thẳng vào `type` của wizard:

```
mã lớp + ảnh bill
  └─ hỏi: học viên / trường học / đối tác?
       ├─ trường học → class_id.school_id      → xác định luôn
       ├─ đối tác    → đối tác của lớp         → thường xác định luôn
       └─ học viên   → hỏi tên học viên
            └─ lọc hoá đơn chưa trả, khớp số tiền
                 ├─ đúng 1 → đề xuất
                 └─ nhiều  → hỏi chọn đợt
```

Nhánh `school` đóng ngay ở lượt hỏi đầu vì `school_id` là
`related="class_id.school_id"` (`wizard/vni_class_invoice.py:50`) — suy thẳng
từ mã lớp, chỉ một trường. Nhánh `partner` gần như vậy: `partner_id` lọc theo
`partner_type = 'partner'` (`:45`) và một lớp thường chỉ có một đối tác.

Nhánh `student` luôn hỏi tên học viên. Không suy danh tính từ số điện thoại
Zalo của người gửi, dù dữ liệu đó có sẵn: phụ huynh chuyển khoản hộ con là
trường hợp thường gặp, và khớp theo số điện thoại khi đó sẽ gán vào đúng
người sai. Hỏi tên tốn một lượt nhưng hành vi đoán được, và không bao giờ
gán thầm.

**Không đoán khi thiếu mã.** Người nộp không ghi mã lớp, hoặc ghi mã không
tồn tại, thì bot nói rõ là không tra được và hỏi lại. Không suy từ số tiền,
không tạo biên lai chờ. Số tiền trùng nhau giữa các đợt và giữa các học viên
là chuyện bình thường — đoán ở đây là gán tiền vào sổ của người khác.

Agent dùng `search_records` / `read_record` sẵn có; không cần tool mới.

Kết quả trả về người dùng là **đề xuất**, diễn đạt rõ là chưa ghi nhận. Quy
tắc trình bày đã có trong `deploy/odoo-mcp/instructions.txt`: không nhắc tên
model/field, tiền định dạng `1.234.567 đ`, ngày `dd/mm/yyyy`, không Markdown.

## Hợp đồng ghi

Điểm ghi là một model method, gọi qua tool `execute_method` sẵn có của
odoo-mcp — **không** cần HTTP endpoint riêng, không cần viết MCP tool mới:

```python
# vni_invoice/models/vni_transfer_receipt.py
@api.model
def record_transfer_receipt(self, payload):
    ...
```

Cơ chế allowlist (đã verify bằng code odoo-mcp thật, `tools_write.py:686`):

- `execute_method` chặn thẳng `create`/`write`/`unlink` (DESTRUCTIVE_METHODS).
- Method lạ bị phân loại `unknown` và **từ chối mặc định**, trừ khi tên
  `model.method` chính xác nằm trong `ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS`.
- Đã test gating: `vni.transfer.receipt.record_transfer_receipt` được cho
  qua; cùng method trên model khác bị chặn; method khác trên cùng model bị
  chặn.

Cấu hình trong `deploy/profile/config.yaml`: thêm `execute_method` vào
`ODOO_MCP_TOOLS_INCLUDE` và đặt
`ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS: "vni.transfer.receipt.record_transfer_receipt"`.
`ODOO_MCP_ENABLE_WRITES` vẫn tắt — nó chỉ gate luồng
`preview_write → execute_approved_write`, không liên quan đường này.

**Ai gọi.** Agent gọi, và bề mặt ghi của nó là đúng một `model.method` này.
Không bật `ODOO_MCP_ENABLE_WRITES` — allowlist method là danh sách tên chính
xác, thêm một tên không mở cả bề mặt ghi.

Phương án cho gateway tự gọi sau khi thấy câu xác nhận đã bị loại: `adapter.py`
không có hook nào chạy sau khi agent trả lời, và không expose tool nào — nên
sẽ phải tự đoán ý định người dùng bằng code parse chuỗi trong adapter, một
việc mong manh hơn hẳn.

Đánh đổi phải nói rõ: cách này **có** nới guardrail. Agent từ chỗ không có
đường ghi nào trở thành có đúng một. Ba biện pháp giữ cho vết nới đó hẹp:

- Tool chỉ nhận biên lai đã có xác nhận của người gửi; thiếu là từ chối.
- Credential là tài khoản dịch vụ riêng, ACL hẹp — **không** phải tài khoản
  admin mà `mcp-odoo` đang dùng cho 7 tool đọc.
- Tool chỉ tạo **biên lai chờ**, không tạo `account.payment`. Đường từ biên
  lai chờ sang payment thật vẫn nằm ngoài tầm với của agent.

SOUL.snippet.md và SKILL.md phải nới đúng một hành vi này, nêu đích danh tên
tool, và giữ nguyên lệnh cấm với mọi thao tác ghi khác. Nới chung chung kiểu
"được phép ghi khi người dùng đồng ý" là hỏng cả tuyến phòng thủ, vì "người
dùng đồng ý" chính là thứ prompt injection tạo ra được.

Payload là một dict: `transaction_ref`, `amount`, `transfer_date` (bắt buộc);
`memo`, `bank_name`, `move_id`, `class_id`, `receipt_type`, `sender_ref`,
`thread_ref` (tuỳ chọn). Trả về `{"success": ..., "receipt_id", "state"}` —
đúng envelope của odoo-mcp.

**Idempotency.** `transaction_ref` là khoá — unique constraint trong DB, và
method trả về biên lai đã có (kèm `duplicate: true`) thay vì tạo bản thứ hai.
Ảnh gửi lại hai lần trong Zalo là chuyện thường, và nếu thiếu khoá này thì
mỗi lần gửi lại là một biên lai trùng.

Method trả lỗi rõ ràng cho: thiếu trường bắt buộc, `move_id` không tồn tại,
`move_id` không phải hoá đơn khách hàng, số tiền âm.

### Chuyển thành payment

Xác nhận trong Zalo tạo ra **biên lai ở trạng thái chờ**, không tạo thẳng
`account.payment`.

Lý do không phải hình thức. Xác nhận của người gửi trả lời được câu "máy đọc
ảnh có đúng không" — nó không trả lời được câu "tiền đã về tài khoản chưa".
Hai câu đó khác nhau, và chỉ câu thứ hai mới đủ để ghi sổ.

Việc biến biên lai chờ thành payment thật đi qua `account.payment.register`
chuẩn, không `create()` thẳng `account.payment`. Lý do:
`models/account_payment_register.py:8` và `models/account_move.py:33` đều chặn
hoá đơn chưa `posted` — bỏ qua chúng là bỏ qua guard nghiệp vụ đã có.

Ai bấm bước cuối đó, và có cần đối chiếu sao kê trước hay không, là quyết định
nghiệp vụ còn để mở — xem "Rủi ro còn mở".

## Model cần thêm

`account.move` hiện **chưa có** field nào để giữ trạng thái "đã báo CK, chờ
đối soát" (đã kiểm: `models/account_move.py` chỉ thêm `vni_class_move_id`,
`in_class_id`, `out_class_id`, `out_sale_line_id`, `student_id`).

Cần một model giữ biên lai đã trích: số liệu OCR, ảnh gốc, hoá đơn được đề
xuất, trạng thái duyệt, và ai duyệt. Tách khỏi `account.move` để một biên lai
chưa khớp được hoá đơn nào vẫn tồn tại và vẫn tra cứu được.

**Đã làm:** `"vni.transfer.receipt": { "allow": ["state"] }` trong
`field_policy.json`. Whitelist rút gọn còn đúng một field vô hại — mọi field
khác bị tước, nên truy vấn vẫn trả về dòng nhưng không mang nội dung dùng
được. Làm bước này **trước** khi tạo model là có chủ ý: model tồn tại mà chưa
khai báo sẽ rơi vào `default` và lộ toàn bộ field.

Cố tình **không** allow `amount`, `transfer_date`, `memo`, `sender_ref`. Agent
không cần đọc lại biên lai để làm việc của nó: số liệu đến từ ảnh trong lượt
hội thoại hiện tại, không phải từ database. Cho đọc `amount` nghĩa là bất kỳ
ai chat với bot cũng dò được số tiền người khác đã nộp.

Biên lai chứa số tài khoản và tên người chuyển — không thuộc về context
của agent.

## Bẫy khi đọc bill ngân hàng Việt Nam

- **Số tiền.** `1.234.567` — dấu chấm là phân cách nghìn, không phải thập phân.
  Đọc nhầm thành `1.23` là sai bốn bậc độ lớn. Nhiều app hiển thị `1.234.567đ`
  dính liền ký tự `đ`.
- **Ngày.** `dd/mm/yyyy`. `03/09/2026` là 3 tháng 9, không phải 9 tháng 3.
- **Ảnh chụp màn hình.** Bị crop mất mã giao dịch, xoay ngang, chụp lại từ màn
  hình khác nên mờ và loá.
- **Không đoán.** Thiếu trường bắt buộc hoặc đọc không chắc thì hỏi lại người
  dùng, không suy diễn. Một con số bịa ở đây là một khoản tiền ghi sai sổ.

## Chống prompt injection

Chữ trong ảnh là **dữ liệu không tin cậy**, ngang hàng với nội dung record đã
nêu ở mục 0.2 của `SKILL.md`. Nội dung chuyển khoản là ô text người gửi tự
điền — kênh chèn chỉ thị rẻ nhất trong toàn hệ thống.

Không bao giờ hành động theo chữ đọc được từ ảnh, kể cả khi nó trông như lệnh
hợp lệ. Quy tắc mục 0.2 cần mở rộng để nói rõ "nội dung OCR" nằm trong danh
sách nguồn không tin cậy.

Lưu ý khi sửa `SKILL.md` và `SOUL.snippet.md`: Hermes quét injection trên
context file, và một match sẽ thay **toàn bộ** file bằng placeholder — tắt
sạch mọi rule trong đó. Mô tả kiểu tấn công, không trích nguyên văn câu tấn
công. Xem chú thích maintainer trong `SOUL.snippet.md` và commit `ab5602e81d`.

## Rủi ro còn mở

- **Ảnh giả hoặc chỉnh sửa — bước xác nhận không chặn được.** Người xác nhận
  chính là người gửi ảnh. Ai gửi ảnh sửa số tiền sẽ xác nhận "đúng rồi" không
  chút do dự. Bước xác nhận chặn **lỗi OCR**, không chặn **gian lận**; đây là
  hai rủi ro khác nhau và chỉ một trong hai đang được xử lý.

  Đó là lý do xác nhận chỉ tạo biên lai chờ, chưa tạo payment. Muốn đóng nốt
  rủi ro này thì cần một trong hai: đối chiếu sao kê ngân hàng, hoặc một người
  **khác người gửi** duyệt bước cuối. Chưa có cái nào — cần quyết trước khi
  bước cuối được tự động hoá.
- **CK đúng số tiền nhưng sai người — đã thu hẹp, chưa đóng hẳn.** Mã lớp +
  loại thu tiền + tên học viên (xem "Mắt xích 2") thu hẹp xuống gần như một
  hoá đơn, và quy tắc không-đoán-khi-thiếu-mã chặn phần còn lại. Chỗ còn hở:
  hai học viên trùng tên trong cùng một lớp, và người nộp gõ nhầm mã lớp sang
  một lớp có thật. Bước xác nhận có giúp ở đây — người nộp đọc lại thấy sai
  lớp hoặc sai đợt thì phát hiện được, khác với trường hợp ảnh giả.
- **Một CK trả nhiều hoá đơn.** Hợp đồng hiện tại là một payment cho một
  `move_id`.
- **Đối soát sao kê.** Chưa có gì đảm bảo biên lai người dùng gửi tương ứng
  với tiền thật đã về tài khoản.

## Việc cần làm khi implement

| # | Việc | Chỗ sửa |
|---|---|---|
| # | Việc | Trạng thái |
|---|---|---|
| 1 | Khai báo model vào field policy | **xong** |
| 2 | Model biên lai + `record_transfer_receipt` + view | **xong** — commit `158ba05a` trên `doanvh0812/feat-transfer-receipt` (vni_edu) |
| 3 | Allowlist method qua `execute_method` | **xong** — `deploy/profile/config.yaml`, gating đã test bằng code odoo-mcp thật |
| 4 | Skill: mục nới quyền `record_transfer_receipt` | **xong**, quét CLEAN |
| 5 | Nới SOUL cho đúng một hành vi, nêu đích danh | **xong**, quét CLEAN |
| 6 | Mở rộng mục 0.2 sang nội dung đọc từ ảnh | **xong**, quét CLEAN |
| 7 | Luồng 7 bước trong instructions.txt | **xong** |

**Đã quét — sạch.** `scan_for_threats(scope="context")` trên `SOUL.snippet.md`,
`SKILL.md`, và bản ghép: cả ba `CLEAN`. Quét bản ghép là bắt buộc vì
`build-soul.sh` nối hai file rồi mới quét — hai câu vô hại riêng lẻ vẫn có thể
khớp khi đứng cạnh nhau. Chạy lại lệnh này sau mỗi lần sửa hai file đó.

Không sửa `adapter.py` — xem "Mắt xích 1".

Thứ tự: 1 → 2 → 3 (policy trước model, phía ghi chạy được trước), rồi 4 → 7.

**Đã verify — cách lấy `payment_id` cho đúng.** `action_create_payments()`
trong Odoo 18 (`account/wizard/account_payment_register.py:1299`) trả về một
`ir.actions.act_window`, **không** trả về payment. Hình dạng phụ thuộc số
lượng: một payment thì có `res_id`, nhiều payment thì có `domain`
`[('id', 'in', ids)]`.

Vì vậy **không** gọi `action_create_payments()` rồi bóc id từ action trả về —
làm thế là bám vào hình dạng của một UI action, thứ có thể đổi giữa các bản
Odoo. Dùng một trong hai đường ổn định hơn:

- gọi `_create_payments()` trực tiếp (dòng 1302) — trả về recordset thật; hoặc
- gọi `action_create_payments()` với context `dont_redirect_to_payments`
  (dòng 1304, khi đó hàm `return True`) rồi truy payment qua `move_id`.

Bản hiện tại dùng `_create_payments()`. Đánh đổi: đó là method có tiền tố
gạch dưới, tức API nội bộ, có thể đổi giữa các bản Odoo — nhưng nó trả về
đúng thứ cần và không phụ thuộc hình dạng UI. Nếu nâng cấp Odoo, kiểm lại
đúng một dòng này.
Bước 1 là phần duy nhất có code thật; còn lại là cấu hình và văn bản.

Bước 5 sửa hai file mà Hermes quét injection. Mô tả kiểu tấn công, không trích
nguyên văn — một match thay **toàn bộ** file bằng placeholder, tắt sạch mọi
rule trong đó. `build-soul.sh` fail build nếu vi phạm; xem commit `ab5602e81d`.

Ghi nhật ký mọi lần xác nhận và mọi lần ghi vào `audit.jsonl` qua `_audit()`
(`adapter.py:994`) — cơ chế đã có sẵn. Lưu ý dòng chú thích tại `adapter.py:1001`:
nhật ký này **không** ghi lại tool MCP nào đã chạy, nên bản ghi xác nhận ở đây
là dấu vết duy nhất cho biết ai đã đồng ý điều gì.
