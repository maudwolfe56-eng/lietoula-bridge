#!/usr/bin/env python3
"""Execute the embedded-data resolver with an in-memory regex quoting correction."""
from __future__ import annotations

from pathlib import Path

source_path = Path(__file__).with_name("resolve_embedded_job_data.py")
lines = source_path.read_text(encoding="utf-8").splitlines()
replacement = r'''pattern = re.compile(rf"""["']{re.escape(key)}["']\s*:\s*["']((?:\\.|[^"']){{1,30000}}?)["']\s*[,}}]""", re.I | re.S)'''
found = False
for index, line in enumerate(lines):
    if "pattern = re.compile(rf'" in line and "re.escape(key)" in line:
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = indent + replacement
        found = True
        break
if not found:
    raise RuntimeError("target regex line not found in embedded resolver source")
source = "\n".join(lines) + "\n"
namespace = {"__name__": "__main__", "__file__": str(source_path), "__package__": None}
exec(compile(source, str(source_path), "exec"), namespace, namespace)
