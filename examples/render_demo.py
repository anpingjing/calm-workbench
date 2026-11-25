"""从公开示例重新生成仓库首页。"""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
template = (root / "04-自动化/workbench_template.html").read_text(encoding="utf-8")
data = json.loads((root / "examples/demo.json").read_text(encoding="utf-8"))
payload = json.dumps(data, ensure_ascii=False).replace("<", r"\u003c")
(root / "index.html").write_text(template.replace("__DATA_JSON__", payload, 1), encoding="utf-8")
