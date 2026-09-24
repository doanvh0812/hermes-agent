---
name: odoo-chat-record-dashboard
description: Trình bày chi tiết MỘT bản ghi (lớp học, đơn hàng, học viên) theo khuôn dashboard quản trị đã được duyệt — kết luận trước, số liệu sau, cảnh báo và việc cần xử lý ở cuối.
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Odoo, Reporting, Zalo, ReadOnly, Vietnamese]
    related_skills: [odoo-chat-support, odoo-chat-performance-reporting, odoo-project-tasks]
---

# Khuôn trả lời chi tiết một bản ghi

Dùng khi người dùng hỏi **chi tiết / thông tin / báo cáo về một đối tượng cụ
thể**: "chi tiết lớp CLASS-00369", "đơn S06047 thế nào", "xem học viên X".

Khác với `odoo-chat-performance-reporting` — skill đó dành cho số liệu **theo
kỳ** và biểu đồ cột. Đây là một bản ghi, không có chuỗi thời gian để vẽ.

## Vì sao có khuôn này

Ngày 22/09/2026 người dùng xem ba cách trình bày cùng một lớp học: danh sách
gạch đầu dòng, bản có biểu tượng và nhóm mục, rồi bản dưới đây. Họ chọn bản
dưới đây và nói **"lần sau hỏi nhớ trả lời thế này nhé"**.

Điểm khiến nó được chọn không phải là biểu tượng, mà là **thứ tự**: người đọc
biết ngay tình trạng, rồi mới tới số, và bất thường nằm ở cuối cùng chứ không
lẫn vào giữa. Giữ thứ tự đó kể cả khi bỏ bớt mục.

## Khuôn

```
BÁO CÁO LỚP | CLASS-00369
Y tế trường học TT 28

1. TRẠNG THÁI CHUNG
🟢 Đã cấp chứng chỉ
Học trực tuyến | 3 học viên | 4 buổi

2. MỐC THỜI GIAN
01/09/2026  Bắt đầu
11/09/2026  Kết thúc
12/09/2026  Thi trực tuyến
15/09/2026  Dự kiến cấp chứng chỉ

3. PHỤ TRÁCH
- Trường đào tạo: CĐ Y Dược Phú Thọ
- Cán bộ quản lý: Cấn Thị Thu Hương
- Giảng viên: Chưa ghi nhận

4. TÀI CHÍNH
- Tổng đơn giá học viên: 3.300.000 đ
- Đã thu: 0 đ
- Hóa đơn khách hàng: 0

5. CẢNH BÁO DỮ LIỆU
🔴 Lớp đã cấp chứng chỉ nhưng chưa có hóa đơn khách hàng
🟠 Có 3 hóa đơn nhà cung cấp nhưng số tiền đã chi là 0 đ
🟡 Chưa tạo lịch học trên hệ thống

6. VIỆC NÊN KIỂM TRA
① Đối chiếu khoản thu và hóa đơn khách hàng
② Kiểm tra trạng thái thanh toán 3 hóa đơn nhà cung cấp
```

## Quy tắc

- **Sáu mục theo đúng thứ tự trên.** Mục nào không có dữ liệu thì bỏ hẳn,
  không giữ tiêu đề rỗng, và không đổi chỗ các mục còn lại.
- **Mức cảnh báo có nghĩa, không trang trí.** 🔴 mâu thuẫn nghiệp vụ (trạng
  thái nói đã xong mà chứng từ nói chưa). 🟠 số liệu không khớp nhau. 🟡 dữ
  liệu còn thiếu nhưng chưa gây sai. Không có gì bất thường thì bỏ mục 5 —
  đừng nặn ra cảnh báo cho đủ khuôn.
- **Mục 6 là việc người đọc làm được**, không phải mô tả lại cảnh báo. Không
  nghĩ ra được việc cụ thể thì bỏ mục.
- Zalo không hiển thị Markdown: không `**`, không `#`, không bảng. Khuôn trên
  là văn bản thuần, căn cột bằng khoảng trắng.
- Tiền `1.234.567 đ`, ngày `dd/mm/yyyy`. Số 0 vẫn phải hiện là `0 đ` — bỏ
  trống làm người đọc tưởng chưa tra.
- Tên nghiệp vụ, không bao giờ tên model hay field (xem `odoo-chat-support`).
- Độ dài vẫn theo giới hạn ~1500 ký tự. Dài quá thì rút mục 2 và 3 trước,
  giữ nguyên 1, 4, 5.

## Khi nào KHÔNG dùng

- Hỏi đúng một con số ("lớp này thu được bao nhiêu") → trả lời thẳng một câu.
  Dựng cả dashboard cho một con số là làm phiền người đọc.
- Danh sách nhiều bản ghi → liệt kê, không phải dashboard cho từng cái.
- Số liệu theo kỳ, so sánh tháng → `odoo-chat-performance-reporting`.
