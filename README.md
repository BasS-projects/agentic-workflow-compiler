# Agentic Workflow Compiler

แปลง `SKILL.md` เป็น Workflow IR ที่ตรวจสอบได้ แล้วรันด้วย Python หรือ LangGraph พร้อม state, audit events และ recovery โดยแยก AI extraction ออกจาก execution

รุ่น **0.2.0** เพิ่ม implementation ครบทั้ง M0–M4 ของ [design roadmap](docs/roadmap.md): semantic review, backend compiler, loop/parallel/approval, HTTP workers, API/UI, scheduling และ RPA ส่วนผลทดสอบจริงและเงื่อนไขที่ยังไม่ได้ตรวจบนบริการภายนอกอยู่ใน [validation record](docs/validation.md)

| Phase | สิ่งที่ใช้งานได้ |
| --- | --- |
| M0 Foundation | Structured Skill → IR0.1, deterministic validation, tools/AI registry, retry/timeout, SQLite resume |
| M1 Semantic compilation | Pending review bundle, source/IR/provenance hashes, diagnostics, constant-condition optimizer, evaluation cases |
| M2 First backend | Executable LangGraph StateGraph artifact สำหรับ IR0.1; ปฏิเสธ semantics ที่ยังไม่รองรับ |
| M3 Advanced execution | IR0.2 bounded foreach/parallel, durable approval/cancellation, central queue, leased HTTP workers, explicit recovery |
| M4 Operations | Authenticated API, web console, interval schedules, metrics, trusted plugins, browser/desktop RPA, Docker Compose |

รันบน Linux หรือ macOS ด้วย Python 3.11+; desktop RPA ใช้ Linux/X11 ผู้ใช้ Windows ใช้ WSL หรือ Linux container ตัวอย่างพื้นฐานไม่ต้องมี API key

## เริ่มใช้งาน

```bash
git clone https://github.com/BasS-projects/agentic-workflow-compiler.git
cd agentic-workflow-compiler
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[langgraph]'

agentic-workflow compile examples/document_pipeline/SKILL.md -o build/workflow.json
agentic-workflow validate build/workflow.json
agentic-workflow run build/workflow.json \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db .state/demo.sqlite3 --run-id document-demo
agentic-workflow inspect document-demo --db .state/demo.sqlite3
agentic-workflow events document-demo --db .state/demo.sqlite3
```

ผลคือ `examples/document_pipeline/output/normalized.txt` และ JSON ที่มี `status: completed` เมื่อต้องการใช้ checkpoint เดิม ให้รันคำสั่ง `run` เดิมเพิ่ม `--resume` โดยคง workflow, input, workspace และ run ID เดิม เปลี่ยน input หรือ workflow ให้ใช้ run ID ใหม่

## เปิด API, worker และ web console

ต้องมี Docker Engine และ Compose บนเครื่องปลายทาง:

```bash
python deploy/init.py
docker compose up --build -d --wait
python deploy/smoke.py --docker
python deploy/init.py --token operator
```

เปิด `http://127.0.0.1:8080` แล้วใส่ operator token ใน console เพื่อ submit และติดตามงาน ใช้ approver token จาก `python deploy/init.py --token approver` สำหรับอนุมัติหรือปฏิเสธ API แยกสิทธิ์ viewer/operator/approver/admin/worker และบันทึก actor จาก credential จริง ไม่ใช้ชื่อที่ผู้ส่งกรอกแทน identity

Compose มี coordinator และ worker แยก process/container พร้อม durable volumes และ health checks ตัว smoke test ส่ง workflow ผ่าน HTTP รอ approval อนุมัติ แล้วตรวจผลที่ worker เขียนจริง อ่าน [operations](docs/operations.md) สำหรับ deployment, token management, backup, scaling, RPA image และ production configuration

GitHub Codespaces ใช้สำหรับพัฒนาได้ผ่าน `.devcontainer` การ deploy ไปยังบัญชี cloud จริงยังต้องระบุเครื่องหรือบริการปลายทาง ข้อมูล TLS และ credentials; repository นี้ไม่สร้าง cloud account หรือเปิดบริการสาธารณะให้โดยอัตโนมัติ

## Compile และ review ก่อนใช้ AI-generated workflow

Structured compilation อ่าน `workflow-ir` fenced JSON เพียงหนึ่ง block โดยไม่เรียก AI เพิ่ม `--bundle` เพื่อเก็บ source/provenance และบังคับ review; เพิ่ม `--optimize` เพื่อ fold เฉพาะ constant conditions โดยไม่ย้ายหรือเรียก tools

```bash
agentic-workflow compile examples/document_pipeline/SKILL.md \
  --bundle --optimize -o build/review.json
# อ่าน source, workflow, provenance และ diagnostics ในไฟล์ก่อนอนุมัติ
agentic-workflow approve-bundle build/review.json --actor workflow-reviewer
agentic-workflow run build/review.json \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db .state/review.sqlite3 --run-id reviewed-demo
```

Semantic extraction ใช้ endpoint/model ที่ระบุเองและคืน review bundle เสมอ:

```bash
agentic-workflow compile examples/semantic/normalize.md \
  --semantic --endpoint http://localhost:8000/v1/chat/completions \
  --model YOUR_MODEL --api-key-env AGENTIC_AI_API_KEY \
  -o build/semantic-review.json
agentic-workflow evaluate examples/semantic/evaluation-cases.json \
  --endpoint http://localhost:8000/v1/chat/completions \
  --model YOUR_MODEL --api-key-env AGENTIC_AI_API_KEY \
  -o .state/live-evaluation.json
```

Endpoint ต้องรองรับ Chat Completions และคืน JSON ตาม protocol คำอธิบายกำกวมหรือ unsupported ต้องถูก reject ผล evaluation ตรวจ exact expected workflow และคืน exit code ไม่เป็นศูนย์เมื่อมี case ไม่ผ่าน การทดสอบด้วย recorded responses พิสูจน์ compiler boundary ได้ แต่ไม่ได้รับรองคุณภาพของโมเดลจริง

Bundle hash ตรวจการแก้ content หลัง review; local review record เป็น acknowledgement ของผู้ใช้ที่เชื่อถือได้ ไม่ใช่ digital signature อ่าน [semantic compilation](docs/semantic-compilation.md) สำหรับ protocol, provenance และ live evaluation

## LangGraph backend

```bash
agentic-workflow run build/workflow.json \
  --backend langgraph --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db .state/langgraph.sqlite3 --run-id graph-demo

agentic-workflow backend-compile build/workflow.json -o build/langgraph
python build/langgraph/run_workflow.py \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db .state/artifact.sqlite3 --run-id artifact-demo
```

StateGraph กำหนดลำดับ execution จริง และใช้ durable step state เพื่อรักษา retry, skip, failure และ resume parity กับ reference runtime รุ่นนี้รองรับ IR0.1 บน LangGraph; IR0.2 ใช้ Python advanced runtime ดู [capability mapping](docs/backends.md)

## Loop, parallel และ approval

IR0.1 เดิมยังใช้ได้ `agentic-workflow migrate build/workflow.json -o build/workflow-v2.json` เปลี่ยน version อย่างชัดเจน IR0.2 เพิ่ม:

- `foreach`: ประมวลผล array ตามลำดับและกำหนด `max_items`
- `parallel`: named branches ที่รันพร้อมกันจริง พร้อม `max_workers`
- `approval`: checkpoint ก่อนขั้นถัดไป สถานะ `waiting_approval` และ CLI exit code 3

```bash
agentic-workflow approve RUN_ID --db .state/runs.sqlite3 \
  --step EXACT_STEP_PATH --actor reviewer --approve
# ใช้ --reject เพื่อปฏิเสธ แล้ว run คำสั่งเดิมพร้อม --resume
agentic-workflow cancel RUN_ID --db .state/runs.sqlite3
```

เส้นทาง approval อ่านจาก `inspect` หรือผล run ส่วน HTTP approval ต้องใช้สิทธิ์ approver ใน API การ resume ใช้ผลขั้นที่เสร็จแล้วและ idempotency key เดิม ดู [IR0.2 semantics](docs/advanced-execution.md), [distributed execution](docs/distributed-execution.md) และ [SIT examples](examples/sit)

## Tools, AI และ RPA

Tool เป็น trusted callable `(args, TaskContext) -> dict` ที่ลงทะเบียนก่อนใช้ IR เลือกได้เฉพาะชื่อ tool ที่ลงทะเบียนและไม่สามารถ import module หรือเรียก shell เอง

Built-ins: `files.exists`, `files.require_exists`, `files.read_text`, `files.write_text`, `text.normalize`, `core.value` File tools จำกัด workspace, ปฏิเสธ symlink/`..` และเขียน UTF-8 แบบ atomic

เพิ่ม `--plugins path/to/plugins.json` ใน local run หรือ worker เพื่อโหลด trusted plugin configuration; `browser.run` ใช้ Playwright และ origin allowlist; `desktop.run` ใช้ xdotool/Pillow บน X11 ดู [tool plugins and RPA](docs/tool-plugins.md)

AI steps แยก registry จาก deterministic tools ใช้ `run --ai-tool TOOL_NAME --endpoint URL --model MODEL --api-key-env ENV_NAME` หรือ worker `--ai-config CONFIG.json` โดยเก็บเพียงชื่อ environment variable ของ credential ใน configuration

## Retry, recovery และขอบเขตความเชื่อถือ

SQLite ของ coordinator อยู่ที่ server เท่านั้น workers ติดต่อผ่าน HTTP และแต่ละ worker มี runtime checkpoint ของตัวเอง Lease ownership และ fencing token ป้องกัน worker เก่าส่งผลมาทับสถานะใหม่ Default เมื่อ lease หมดคือ `needs_recovery`; ผู้ดำเนินการต้องตรวจ external effects ก่อน retry

```bash
agentic-workflow recover RUN_ID --db .state/runs.sqlite3 --retry-interrupted
```

Retry/timeout/cancellation ไม่ rollback external effects และไม่รับประกัน exactly-once ปลายทาง Tool ที่มี side effects ต้องใช้ idempotency key หรือ reconciliation ของระบบปลายทางเอง อนุมัติที่ค้างจะผูกกับ worker เดิมเพื่อรักษา local checkpoint; ย้าย worker ผ่าน explicit recovery ซึ่งอาจต้องทำบางขั้นซ้ำ

Subprocess isolation ใช้ควบคุม execution ไม่ใช่ sandbox สำหรับ untrusted Python plugins เก็บ runtime data, auth files และ workspace ภายใต้สิทธิ์ระบบที่เหมาะสม อย่าใส่ secrets ใน workflow inputs/outputs เพราะ state และ events เก็บข้อมูลเหล่านั้น

## ทดสอบด้วยตัวเอง

```bash
python -m pip install -e '.[all]'
python -m unittest discover -s tests -v
python -m playwright install chromium
python -m sit.run --output .state/sit-report.json
```

Desktop scenario ต้องมี Xvfb/X11 และ xdotool; บน Linux ใช้ `xvfb-run -a python -m sit.run --output .state/sit-report.json` Runner เก็บ JSON, JUnit และหลักฐาน พร้อมแยก passed/failed/skipped และคืน nonzero เมื่อมี failure ดู [SIT plan](docs/sit-plan.md) สำหรับ scenario, oracle และ dependency gates

[GitHub Actions](https://github.com/BasS-projects/agentic-workflow-compiler/actions) รัน Python matrix, SIT พร้อม browser/desktop จริง และ Docker deployment smoke test [Validation record](docs/validation.md) ระบุสิ่งที่รันสำเร็จจริงและข้อจำกัดของ environment

เอกสารเพิ่มเติม: [architecture](docs/architecture.md), [roadmap](docs/roadmap.md), [ADRs](docs/adr), [cloud development](docs/cloud-development.md)

[MIT License](LICENSE)
