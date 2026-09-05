# ADR 0001: Start with explicit structured IR

Status: accepted for MVP

## Context

เป้าหมายคือแปลง skill เป็น workflow แต่การตีความภาษาธรรมชาติมีความกำกวม และความถูกต้องของ execution engine ควรตรวจได้โดยไม่ขึ้นกับการเปลี่ยน model หรือ provider

## Decision

Default extractor อ่าน `workflow-ir` fenced JSON block หนึ่ง block ใน `SKILL.md` เท่านั้น Protocol `SemanticExtractor.extract(text) -> dict` เปิดให้เลือก semantic provider และผล extraction ทุกแบบผ่าน deterministic validator เดียวกัน มี optional `ChatCompletionsProvider` ที่ใช้ endpoint/model ของผู้เรียกเอง โดยต้องเลือกใช้ผ่าน Python API หรือ CLI `--semantic` อย่างชัดเจน

Parser ไม่ execute งาน และไม่ใช้ heuristic แอบแปลง prose เป็น workflow

## Consequences

MVP รันตัวอย่างและทดสอบได้โดยไม่ใช้ credentials Default mode ใช้ IR ที่ผู้เขียนระบุเอง ส่วน semantic mode มี HTTP implementation และ mocked boundary tests แต่ยังไม่ทดสอบ live endpoint/model การรองรับ protocol จึงไม่รับประกันความแม่นยำในการตีความ prose ของ provider
