#!/usr/bin/env python3
"""Launch ``resolve_embedded_job_data.py`` after correcting its regex source line.

This keeps the original implementation auditable while avoiding a quoting ambiguity in a
raw f-string. The correction is applied only in memory; public-source and review policies
remain unchanged.
"""
from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).with_name("resolve_embedded_job_data.py")
lines = SOURCE.read_text(encoding="utf-8").splitlines()
replaced = False
for index, line in enumerate(lines):
    if "pattern = re.compile(rf'" in line and "re.escape(key)" in line:
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = indent + 'pattern = re.compile(rf"[\\\"\'\"]{re.escape(key)}[\\\"\'\"]\\s*:\\s*[\\\"\'\"]((?:\\\\.|[^\\\"\'\"]){{1,30000}}?)[\\\"\'\"]\\s*[,}}]", re.I | re.S)'
        replaced = True
        break
if not replaced:
    raise RuntimeError("embedded resolver regex source line was not found")
source = "\n".join(lines) + "\n"
namespace = {
    "__name__": "__main__",
    "__file__": str(SOURCE),
    "__package__": None,
}
exec(compile(source, str(SOURCE), "exec"), namespace, namespace)
