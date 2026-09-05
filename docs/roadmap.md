# Roadmap

สถานะนี้แยกสิ่งที่ MVP ทำได้ออกจากแนวทางพัฒนาต่อ รายการในอนาคตยังไม่ใช่ commitment เรื่องกำหนดส่งหรือ backend ที่เลือกแล้ว

## Milestone 0 — runnable foundation

ขอบเขตของ repository รุ่นแรก:

- `SKILL.md` ที่มี structured `workflow-ir` JSON → compile → deterministic validation
- Python runtime สำหรับ sequential tasks, condition, references, retry และ timeout
- Registered tool และ explicit AI callable extension points พร้อม optional OpenAI-compatible HTTP provider
- SQLite state/events, resume, stable idempotency keys และ explicit recovery ของ run ที่ถูกขัดจังหวะ
- Example document pipeline ที่รันได้โดยไม่ใช้ API key
- CLI, tests, CI และ development container สำหรับ cloud development
- Semantic extraction protocol และ optional `ChatCompletionsProvider` พร้อม mocked HTTP tests; backend adapter เป็น protocol โดยยังไม่มี remote backend implementation

Acceptance: clone repository, สร้าง Python environment, compile ตัวอย่าง, รันจนได้ output file, inspect/events ได้ และ resume แล้วไม่รันขั้นที่สำเร็จซ้ำ

## Milestone 1 — semantic extraction ที่ตรวจสอบได้

- ทดสอบ `ChatCompletionsProvider` กับ endpoint/model จริง โดยรักษา `SemanticExtractor` boundary
- ให้ extraction คืน IR ตาม schema แล้วผ่าน deterministic validator ทุกครั้ง
- เพิ่ม evaluation cases ที่มี expected workflow และกรณีคำอธิบายกำกวม
- แสดง IR เพื่อ review ก่อน execution ของงานที่สร้างด้วย semantic extraction
- บันทึก source/provenance, model configuration และ compile diagnostics โดยไม่เก็บ credentials
- พิจารณา optimizer เฉพาะ transformation ที่พิสูจน์ได้ว่าไม่เปลี่ยนความหมาย

Acceptance: วัดความถูกต้องของการแปลง skill กับชุดตัวอย่างได้ และ unsupported/ambiguous instructions ไม่ถูก execute โดยการเดา

## Milestone 2 — backend compiler ตัวแรก

- เลือก backend จาก use case จริงระหว่าง Temporal, n8n, GitHub Actions, LangGraph หรือ Azure Durable Functions
- กำหนด capability mapping และ reject unsupported semantics
- เขียน adapter ที่สร้าง artifact ของ backend พร้อม validation
- เปรียบเทียบผลกับ Python reference runtime ใน workflow ชุดเดียวกัน

Acceptance: workflow ที่ประกาศว่ารองรับต้องได้ behavior ที่สอดคล้องกัน รวม failure, retry และ condition โดยไม่ลด semantics เงียบ ๆ

## Milestone 3 — execution capabilities เพิ่มเติม

พิจารณา loops, parallel tasks, human approvals และ remote workers หลังจากกำหนด semantics เรื่อง state, cancellation, retry และ idempotency ของแต่ละ feature แล้ว แต่ละ feature ควรเป็นการเปลี่ยน IR version ที่ระบุ migration ชัดเจน

Distributed execution ต้องมี durable queue/locking, worker ownership และ recovery semantics ที่ออกแบบเฉพาะ ไม่ต่อยอดโดยสมมติว่า SQLite file lock ใช้แทน distributed coordinator ได้

## Milestone 4 — interaction และ operations

พิจารณา API/UI, scheduling, browser/desktop RPA, deployment และ observability ตาม workflow ที่ใช้งานจริง แยก credential management และ approval ของ action ออกจาก workflow source

## Decisions still open

- Repository visibility และแนวทางการดูแล release โดย source ใช้ MIT License
- Endpoint/model สำหรับ semantic extraction ที่จะตรวจ compatibility จริง และ backend compiler ตัวแรก
- แนวทาง human approvals และ authorization สำหรับ external side effects
- Storage และ deployment target เมื่อมี long-running production service
