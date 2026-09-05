# ADR 0003: SQLite state with explicit crash recovery

Status: accepted for MVP

## Context

MVP ต้องรันบนเครื่องเดียวได้ง่าย พร้อมรู้ว่าแต่ละ step สำเร็จหรือยัง และสามารถต่อจากงานที่ล้มเหลวโดยไม่รันทุกขั้นใหม่ External side effects ไม่สามารถ commit เป็น transaction เดียวกับ SQLite ได้ทั่วไป

## Decision

ใช้ SQLite เก็บ workflow identity, normalized inputs, workspace, run/step results และ events ใช้ POSIX per-run file lock เพื่อกัน executor บนเครื่องเดียวกันรัน run เดียวกันซ้อน Resume reuse completed steps และตรวจ identity เดิม

Idempotency key คงที่ต่อ run/step เพื่อให้ callable ส่งต่อไปยังระบบที่รองรับ deduplication ได้ Retry budget เริ่มใหม่ต่อ resume invocation แต่เก็บ total attempts ไว้

หาก crash ทิ้งสถานะ running ที่ไม่ทราบผล ต้องให้ caller ตรวจ side effects และเรียก explicit recovery ด้วย policy `retry` ก่อน resume

## Consequences

ไม่ต้องติดตั้ง database server สำหรับเริ่มต้น แต่ยังไม่รองรับ distributed execution การมี SQLite และ idempotency key ไม่รับรอง exactly-once external effects Recovery อาจทำให้ external action ซ้ำถ้า provider ไม่รองรับ idempotency Timeout หรือ process kill ไม่ rollback action ที่เกิดแล้ว

การใช้ POSIX lock และ subprocess ทำให้ MVP มุ่ง Linux/macOS ผู้ใช้ Windows ใช้ WSL หรือ Linux container
