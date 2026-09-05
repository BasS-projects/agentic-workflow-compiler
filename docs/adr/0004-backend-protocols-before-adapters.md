# ADR 0004: Backend protocols before remote adapters

Status: accepted for MVP

## Context

แนวคิดเดิมรองรับหลาย execution backends แต่แต่ละระบบมี semantics ของ retry, timeout, cancellation และ persistence ต่างกัน การประกาศว่ารองรับตั้งแต่แรกโดยยังไม่มี behavioral mapping จะทำให้ workflow portability ไม่น่าเชื่อถือ

## Decision

ให้ Python runtime เป็น reference implementation ของ MVP มี `BackendAdapter` protocol และ capability declarations เท่านั้น ยังไม่ implement compiler/deployment ไป remote backend

เริ่มด้วย task sequence, condition, immutable variables, retry, timeout, tool และ explicit AI ไม่ใส่ loops, parallel execution, distributed execution, scheduling, UI หรือ RPA ใน IR รุ่นแรก

## Consequences

ขอบเขตทดสอบได้ชัดเจนและไม่ผูกกับ vendor Backend ใหม่ต้องประกาศ capability และปฏิเสธ unsupported semantics การรองรับ Temporal, n8n, GitHub Actions, LangGraph หรือ Azure Durable Functions ยังเป็นงานในอนาคต
