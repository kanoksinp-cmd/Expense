# Trip Expense Splitter Pro — Render.com Deploy Guide

## โครงสร้างไฟล์
```
trip-splitter/
├── app.py              ← Flask server + REST API
├── requirements.txt    ← Python dependencies
├── render.yaml         ← Render deploy config
└── templates/
    └── index.html      ← Frontend (HTML+CSS+JS)
```

## วิธี Deploy บน Render.com (ฟรี)

### ขั้นตอนที่ 1 — อัปโหลดขึ้น GitHub
1. สร้าง repo ใหม่บน [github.com](https://github.com)
2. อัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้น repo

### ขั้นตอนที่ 2 — Deploy บน Render
1. เข้า [render.com](https://render.com) → สมัครฟรี
2. กด **New → Web Service**
3. เลือก repo ที่สร้างไว้
4. ตั้งค่าดังนี้:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`
5. เพิ่ม **Disk** (เพื่อเก็บ SQLite ถาวร):
   - ไปที่ Tab **Disks** → Add Disk
   - Name: `db-storage`
   - Mount Path: `/opt/render/project/src`
   - Size: 1 GB
6. เพิ่ม **Environment Variable:**
   - Key: `DB_PATH`
   - Value: `/opt/render/project/src/trip_database.db`
7. กด **Deploy**

### ขั้นตอนที่ 3 — เข้าใช้งาน
รอประมาณ 2-3 นาที แล้วเปิด URL ที่ Render ให้มา เช่น:
`https://trip-expense-splitter.onrender.com`

แชร์ URL นี้ให้เพื่อนร่วมทริปเข้าใช้งานพร้อมกันได้เลย!

---

## หมายเหตุ Render Free Plan
- เซิร์ฟเวอร์จะ sleep หลังไม่มีคนใช้ 15 นาที (wake up ครั้งแรกช้าประมาณ 30-60 วินาที)
- ข้อมูล SQLite จะอยู่ถาวรใน Disk ที่ mount ไว้
- ถ้าต้องการให้ตื่นตลอดเวลา ต้องอัปเกรดเป็น paid plan ($7/เดือน)
