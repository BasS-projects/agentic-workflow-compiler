# Agentic Workflow Compiler

แปลง `SKILL.md` ให้เป็น Workflow IR ที่ตรวจสอบได้ แล้วรันตามลำดับด้วย Python พร้อมบันทึกสถานะใน SQLite เพื่อดูผลย้อนหลังและทำงานต่อจากขั้นที่ยังไม่สำเร็จ

โปรเจกต์นี้เริ่มจากแนวคิด **Skill → extraction → IR → deterministic validation → runtime → result/logs** โดยแยกการตีความด้วย AI ออกจากการควบคุม execution อย่างชัดเจน เป้าหมายระยะยาวคือระบบที่ไม่ผูกกับผู้ให้บริการ AI หรือ workflow backend รายใดรายหนึ่ง

## สถานะ MVP

ใช้งานเป็น Python CLI บน Linux หรือ macOS ได้ มีการตั้งค่า development container สำหรับทำงานใน GitHub Codespaces โปรเจกต์ยังไม่มีบริการ API, หน้าเว็บ หรือ remote workflow engine ที่ deploy ให้แล้ว ผู้ใช้ Windows ใช้ WSL หรือ Linux container

MVP ใช้ Python 3.11 ขึ้นไปและ standard library สำหรับ runtime ไม่ต้องมี API key เพื่อรันตัวอย่าง

| มีใน MVP | ขอบเขต |
| --- | --- |
| Skill extraction | อ่าน JSON จาก fenced block `workflow-ir` หนึ่ง block ใน `SKILL.md` |
| `SemanticExtractor` | Protocol และ optional OpenAI-compatible HTTP provider สำหรับสร้าง IR แล้วส่งผ่าน validator เดียวกัน |
| Workflow IR | task ตามลำดับ, input, reference, immutable variable, `when`, retry, timeout, output |
| Python runtime | registered tools, explicit AI steps, optional HTTP provider, subprocess timeout, sequential execution |
| State และ logs | SQLite, resume, inspect, event log, idempotency key ต่อ run/step |
| Backend adapter | Protocol และ capability declarations สำหรับขยายต่อ |

ค่าเริ่มต้นอ่าน IR ที่เขียนไว้ชัดเจนในเอกสารและไม่เรียก AI มี `ChatCompletionsProvider` สำหรับ semantic extraction และ AI task ผ่าน OpenAI-compatible HTTP endpoint เมื่อระบุ endpoint/model เอง การเชื่อมต่อ provider ทดสอบด้วย mocked HTTP boundary แล้ว แต่ยังไม่ได้ทดสอบกับบริการ AI จริง

## เริ่มใช้งาน

รันจากโฟลเดอร์ repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

mkdir -p build
python -m agentic_workflow compile examples/document_pipeline/SKILL.md -o build/workflow.json
python -m agentic_workflow validate build/workflow.json
python -m agentic_workflow run build/workflow.json \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db build/runs.sqlite3 \
  --run-id document-demo
```

เมื่อสำเร็จ ไฟล์ `examples/document_pipeline/output/normalized.txt` จะมีข้อความที่ผ่าน `text.normalize` และ CLI จะคืน JSON ที่มี `run_id`, `status` และ `outputs` ชื่อ run ต้องไม่ซ้ำสำหรับการเริ่มงานใหม่

ตรวจสอบสถานะและ event log:

```bash
python -m agentic_workflow inspect document-demo --db build/runs.sqlite3
python -m agentic_workflow events document-demo --db build/runs.sqlite3
```

สั่ง resume ด้วย workflow, input และ workspace เดิม:

```bash
python -m agentic_workflow run build/workflow.json \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db build/runs.sqlite3 \
  --run-id document-demo \
  --resume
```

ขั้นที่สำเร็จแล้วจะใช้ผลเดิม หาก run สำเร็จครบแล้ว การ resume จะคืนผลที่บันทึกไว้ การ resume ไม่ใช่การเริ่มงานใหม่หลังแก้ไฟล์ input: เมื่ออยากประมวลผลข้อมูลใหม่ให้ใช้ run ID ใหม่

## ทำงานบน cloud

เปิด [repository บน GitHub](https://github.com/BasS-projects/agentic-workflow-compiler) แล้วเลือก **Code → Codespaces → Create codespace** เพื่อใช้ development environment จาก `.devcontainer/devcontainer.json` จากนั้นรันคำสั่ง quickstart ใน terminal ของ Codespace

Codespaces เป็นเครื่องสำหรับพัฒนาและรัน CLI งานตามคำสั่งใน MVP นี้ ยังไม่มี scheduler หรือ worker service ที่ทำงานต่อโดยอัตโนมัติเมื่อ Codespace หยุด เก็บ source ใน Git และสำรอง SQLite กับไฟล์ output แยกตามความต้องการก่อนลบ environment ดู [`docs/cloud-development.md`](docs/cloud-development.md) สำหรับ cloud environment และการนำ repository ขึ้น GitHub

## รูปแบบ `SKILL.md`

เขียนคำอธิบายงานตามปกติ และใส่ IR ใน fenced block ที่ติดชื่อ `workflow-ir` เพียงหนึ่ง block:

````markdown
# Greeting

ส่งข้อความจาก input เป็น output

```workflow-ir
{
  "ir_version": "0.1",
  "id": "greeting",
  "inputs": {
    "name": {"type": "string", "default": "world"}
  },
  "steps": [
    {
      "id": "message",
      "kind": "tool",
      "tool": "core.value",
      "args": {"value": {"$ref": "inputs.name"}}
    }
  ],
  "outputs": {"name": {"$ref": "steps.message.value"}}
}
```
````

`steps` คือ sequence โดยตรง การอ้างอิงต้องใช้ object `{"$ref": "..."}` ทั้งก้อน รองรับ input และ output ของขั้นก่อนหน้าเท่านั้น ไม่มี expression evaluation, arbitrary import หรือ shell execution ใน IR

ตัวแปรเป็นค่าจาก input และผลของแต่ละ step ที่ไม่แก้ย้อนหลัง ใช้ `core.value` เมื่อต้องการตั้งชื่อค่าระหว่างทาง `when` ใช้ condition เช่น `{"equals": [{"$ref": "inputs.use_ai"}, true]}` ขั้นที่ถูกข้ามไม่มีผลลัพธ์สำหรับให้ขั้นถัดไปใช้เป็นข้อมูลที่จำเป็น

## ตัวอย่าง document pipeline

ตัวอย่างใน [`examples/document_pipeline/SKILL.md`](examples/document_pipeline/SKILL.md) ทำงานดังนี้:

1. รับ `input_path`, `output_path` และ `use_ai` จาก input JSON
2. ตรวจว่าไฟล์ input มีอยู่ แล้วอ่านเป็น UTF-8
3. ประมวลผลข้อความด้วย `text.normalize`: ตัด whitespace หัวท้ายของแต่ละบรรทัด ตัดบรรทัดว่างหัวท้ายเอกสาร และใช้ line ending แบบ LF โดยคงช่องว่างภายในบรรทัดไว้
4. มีขั้น AI สำหรับผลสรุปเสริม โดย `use_ai` เป็น `false` ตามค่าเริ่มต้น
5. เขียนผลจากขั้น normalize ลงไฟล์เสมอ จึงไม่อ้างอิงข้อมูลจาก AI step ที่อาจถูกข้าม
6. สร้าง completion record และให้ runtime บันทึกสถานะกับ events

หากเปลี่ยน `use_ai` เป็น `true` ต้องลงทะเบียน provider ชื่อ `document.summarize` ผ่าน Python API หรือ CLI flags ตามตัวอย่างถัดไป

## ใช้ AI provider แบบ optional

ต้องมี endpoint ที่รองรับ Chat Completions อยู่แล้ว โดยส่ง **URL เต็ม** รวม `/v1/chat/completions` และ model identifier ที่ endpoint นั้นรองรับ ตัวอย่างนี้ใช้ localhost และ `YOUR_MODEL` เป็น placeholder:

```bash
python -m agentic_workflow compile examples/document_pipeline/SKILL.md \
  -o build/semantic-workflow.json \
  --semantic \
  --endpoint http://localhost:8000/v1/chat/completions \
  --model YOUR_MODEL
```

`--semantic` ส่งข้อความ skill ให้ provider เพื่อสร้าง IR แล้วตรวจด้วย validator เมื่อไม่ใส่ flag นี้ compiler จะใช้ structured block แบบ offline ตามเดิม ควรอ่าน IR ที่สร้างใน `build/semantic-workflow.json` ก่อนเลือกนำไปรัน เพราะ validation ตรวจโครงสร้าง ไม่ได้ยืนยันว่า AI เข้าใจเจตนาถูกต้อง

สำหรับ AI task ใน document example ให้แก้ `use_ai` ใน `examples/document_pipeline/inputs.json` เป็น `true` แล้วใช้ run ID ใหม่:

```bash
python -m agentic_workflow run build/workflow.json \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline \
  --db build/runs.sqlite3 \
  --run-id document-ai-demo \
  --ai-tool document.summarize \
  --endpoint http://localhost:8000/v1/chat/completions \
  --model YOUR_MODEL
```

ถ้า endpoint ต้องใช้ API key ให้ตั้ง environment variable เอง แล้วเพิ่ม `--api-key-env AGENTIC_AI_API_KEY` โดย flag นี้รับชื่อ environment variable เพื่อให้ credentials ไม่อยู่ใน workflow source

## ต่อ tool หรือ AI provider

Tool เป็น Python callable รูปแบบ `(args: dict, context: TaskContext) -> dict` และต้องลงทะเบียนก่อนใช้ Workflow เลือกเรียกเฉพาะชื่อที่ลงทะเบียนได้ ผลลัพธ์ต้อง serialize เป็น JSON ได้

```python
from agentic_workflow.parser import compile_skill
from agentic_workflow.providers import ChatCompletionsProvider
from agentic_workflow.runtime import Runtime


provider = ChatCompletionsProvider(
    endpoint="http://localhost:8000/v1/chat/completions",
    model="YOUR_MODEL",
    timeout_seconds=30,
)


with open("examples/document_pipeline/SKILL.md", encoding="utf-8") as source:
    workflow = compile_skill(source.read())

runtime = Runtime(
    db_path="build/ai-runs.sqlite3",
    workspace="examples/document_pipeline",
    ai_tools={"document.summarize": provider},
)
```

ตัวอย่างข้างบนสร้าง runtime ที่ลงทะเบียน provider แล้ว แต่ยังไม่เรียก `run` หรือส่ง HTTP request ต้องมี model ที่ endpoint นั้นรองรับก่อนใช้งานจริง และสามารถแทน provider ด้วย trusted callable ของตัวเองได้

การเปลี่ยนจาก structured extraction เป็น semantic extraction ใช้ `compile_skill(text, extractor=provider)` หรือ object อื่นที่มี `extract(text: str) -> dict` ตาม `SemanticExtractor` protocol ผล extraction ทุกแบบต้องผ่าน deterministic validator ก่อน execution อ่านรายละเอียดใน [`docs/architecture.md`](docs/architecture.md)

## Resume, retry และ side effects

Runtime บันทึก workflow, inputs, workspace และผลของแต่ละ step ใน SQLite การ resume ต้องรักษา identity เหล่านี้ไว้ ขั้นที่สำเร็จหรือถูก skip แล้วถูก reuse และ idempotency key ของ run/step เดิมคงเดิม การ resume แต่ละครั้งให้ retry budget ใหม่ตาม config ของ step โดยเก็บจำนวน attempts ที่เกิดขึ้นทั้งหมดไว้ Identity นี้ยังไม่รวมเวอร์ชัน source ของ custom tool/provider ผู้เรียกจึงควรคง implementation เดิมเมื่อ resume

ถ้า process ล่มขณะ step กำลังทำงาน ระบบอาจยังไม่ทราบว่า side effect เสร็จหรือไม่ ต้องตรวจสอบไฟล์หรือระบบปลายทางก่อนสั่งให้ retry ขั้นที่ค้าง:

```bash
python -m agentic_workflow recover document-demo \
  --db build/runs.sqlite3 \
  --retry-interrupted
```

จากนั้นใช้คำสั่ง `run ... --run-id document-demo --resume` เดิม การ recover เป็นการยืนยันว่าจะลองขั้นที่ถูกขัดจังหวะใหม่ ไม่สามารถ rollback side effect หรือยืนยัน exactly-once ให้ external service ได้

Timeout หยุด subprocess ที่รัน step จึงหยุด computation ที่ค้างได้ แต่ไม่เรียกคืน HTTP request หรือ external effect ที่ส่งออกไปก่อนหน้า SQLite และ per-run file lock ป้องกันสอง executor รัน run เดียวกันภายในเครื่องและ shared state ที่รองรับ lock นี้ ระบบยังไม่ใช่ distributed execution engine

Tool ต้องทำงานจนเสร็จก่อนคืนผลและไม่เริ่ม background job ที่ต้องทำงานต่อหลังคืนค่า เพราะ runtime จะปิด process group หลังจบ step

## ขอบเขตไฟล์และความเชื่อถือ

Built-in file tools จำกัด path ให้อยู่ใต้ `--workspace` ปฏิเสธ symlink และ path component `..` ใช้ UTF-8 และเขียนไฟล์แบบ atomic Path ที่คืนจาก tool เป็น path เทียบกับ workspace การเรียก tool ที่ลงทะเบียนเองเป็นการรันโค้ดที่ผู้พัฒนาเชื่อถือ subprocess และ path guard ของ built-in tools ไม่ใช่ sandbox สำหรับโค้ดอันตราย

อย่าใส่ secret เป็น input หรือ output หากไม่ต้องการให้เก็บอยู่ใน SQLite และ logs ควรอ่าน credentials จาก environment ภายใน provider ที่เลือกใช้

## ยังไม่อยู่ใน MVP

ไม่มี loops, parallel execution, distributed workers, scheduling, UI, desktop/browser RPA หรือ backend compiler ที่ deploy ไปยัง Temporal, n8n, GitHub Actions, LangGraph หรือ Azure Durable Functions รายชื่อ backend เหล่านี้เป็นทิศทางการพัฒนาต่อ ไม่ใช่ integration ที่ใช้งานได้ในโค้ดปัจจุบัน

ดู [`docs/architecture.md`](docs/architecture.md), [`docs/roadmap.md`](docs/roadmap.md) และ [`docs/adr`](docs/adr) สำหรับเหตุผลการออกแบบและลำดับพัฒนา

เผยแพร่ภายใต้ [MIT License](LICENSE)
