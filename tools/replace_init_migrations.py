"""Replace inline migration block in __init__.py with run_startup_migrations call."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "app" / "__init__.py"
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
start = next(i for i, l in enumerate(lines) if "Migration: ensure necessary columns" in l)
end = next(
    i
    for i, l in enumerate(lines)
    if 'print("--- Migration abgeschlossen ---")' in l and i > start
)
while end + 1 < len(lines) and lines[end + 1].strip() in ("conn.close()", ""):
    end += 1
replacement = [
    "    from app.startup_migrations import run_startup_migrations\n",
    "    run_startup_migrations(app)\n",
    "\n",
]
new_lines = lines[:start] + replacement + lines[end + 1 :]
p.write_text("".join(new_lines), encoding="utf-8")
print(f"Replaced lines {start + 1}-{end + 1}")
