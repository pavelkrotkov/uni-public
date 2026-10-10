import json
from pathlib import Path


def render_report(template_path: Path, view_model: dict[str, object]) -> str:
    html = template_path.read_text(encoding="utf-8")
    for key, value in view_model.items():
        value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        html = html.replace(f"__{key}__", value)
    return html
