import base64
import pathlib
import urllib.request

BASE = pathlib.Path(__file__).parent

FILES = [
    "diagram1_current_coupling.mmd",
    "diagram2_proposed_layers.mmd",
    "diagram3_startup_sequence.mmd",
    "diagram4_dependency_before_after.mmd",
]

for name in FILES:
    src = BASE / name
    text = src.read_text(encoding="utf-8")
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/svg/{encoded}?theme=default"
    out = BASE / (src.stem + ".svg")
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    out.write_bytes(data)
    print(f"{name} -> {out.name} ({len(data)} bytes)")
