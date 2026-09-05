# ADR 0002: Explicit AI tasks and registered callables

Status: accepted for MVP

## Context

Workflow ที่เรียก AI อัตโนมัติทุกขั้นยากต่อการตรวจลำดับ ค่าใช้จ่าย และ failure behavior ขณะเดียวกัน IR ที่ import code หรือ execute expression ได้เองจะทำให้ validation ไม่สามารถกำหนด execution surface ได้ชัดเจน

## Decision

Task ประกาศ `kind: "tool"` หรือ `kind: "ai"` และเรียก callable ที่ลงทะเบียนด้วยชื่อ ทั้งสองใช้ `(args, TaskContext) -> dict` แต่มี registry แยกกัน IR ไม่มี arbitrary import, shell command หรือ `eval`

Provider implementation เป็น trusted Python code ที่ผู้พัฒนาเลือกเอง มี optional `ChatCompletionsProvider` สำหรับ HTTP endpoint/model ที่ผู้เรียกระบุ โดยยังไม่มี provider/model ที่เชื่อมต่อไว้ตามค่าเริ่มต้น และยังไม่ทดสอบกับบริการ AI จริง

## Consequences

ผู้ตรวจ workflow มองเห็นจุดที่ใช้ AI และ runtime control flow ทดสอบได้โดยไม่มี model การเป็น deterministic control flow ไม่ได้รับรองว่า tool output หรือ model output จะเหมือนเดิมทุกครั้ง Custom tools ยังคงมีสิทธิ์ของ process จึงไม่ใช่ sandbox สำหรับ untrusted code
