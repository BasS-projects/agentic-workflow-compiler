# Document pipeline

รับ path ของไฟล์ข้อความ ตรวจว่าไฟล์มีอยู่ อ่านและ normalize ข้อความ แล้วเขียนไฟล์ผลลัพธ์พร้อม completion record

ขั้น `summarize_optional` เป็น explicit AI task สำหรับผลสรุปเสริม ปิดไว้ตามค่าเริ่มต้น ตัวอย่างจึงรันได้โดยไม่มี AI provider หรือ credentials หากเปิด `use_ai` ต้องลงทะเบียน Python callable ชื่อ `document.summarize` ผ่าน `Runtime(ai_tools=...)` หรือ CLI `--ai-tool document.summarize` พร้อม endpoint/model ก่อน

ไฟล์ output ใช้ข้อความจาก `normalize_text` โดยตรง ไม่พึ่ง AI step ที่อาจถูก skip ผล AI เมื่อเปิดใช้สามารถดูใน persisted step state ได้ Runtime บันทึก events และสถานะของทุกขั้นใน SQLite

ทุก file path เป็น path ภายใต้ workspace ที่ส่งให้ runtime

```workflow-ir
{
  "ir_version": "0.1",
  "id": "document_pipeline",
  "inputs": {
    "input_path": {"type": "string"},
    "output_path": {"type": "string", "default": "output/normalized.txt"},
    "use_ai": {"type": "boolean", "default": false}
  },
  "steps": [
    {
      "id": "validate_input",
      "kind": "tool",
      "tool": "files.require_exists",
      "args": {"path": {"$ref": "inputs.input_path"}},
      "timeout_seconds": 10
    },
    {
      "id": "read_input",
      "kind": "tool",
      "tool": "files.read_text",
      "args": {"path": {"$ref": "inputs.input_path"}},
      "retry": {"max_attempts": 2, "delay_seconds": 0},
      "timeout_seconds": 10
    },
    {
      "id": "normalize_text",
      "kind": "tool",
      "tool": "text.normalize",
      "args": {"text": {"$ref": "steps.read_input.text"}},
      "timeout_seconds": 10
    },
    {
      "id": "summarize_optional",
      "kind": "ai",
      "tool": "document.summarize",
      "when": {"equals": [{"$ref": "inputs.use_ai"}, true]},
      "args": {
        "text": {"$ref": "steps.normalize_text.text"},
        "instruction": "Summarize the document in two concise sentences."
      },
      "retry": {"max_attempts": 1, "delay_seconds": 0},
      "timeout_seconds": 30
    },
    {
      "id": "write_output",
      "kind": "tool",
      "tool": "files.write_text",
      "args": {
        "path": {"$ref": "inputs.output_path"},
        "text": {"$ref": "steps.normalize_text.text"}
      },
      "timeout_seconds": 10
    },
    {
      "id": "record_completion",
      "kind": "tool",
      "tool": "core.value",
      "args": {
        "value": {
          "message": "Document pipeline completed",
          "output_path": {"$ref": "steps.write_output.path"},
          "bytes_written": {"$ref": "steps.write_output.bytes"}
        }
      },
      "timeout_seconds": 10
    }
  ],
  "outputs": {
    "output_path": {"$ref": "steps.write_output.path"},
    "bytes_written": {"$ref": "steps.write_output.bytes"},
    "completion": {"$ref": "steps.record_completion.value"}
  }
}
```
