"""Offline structure checks (a subset of hassfest/HACS checks) for local runs.

Run: python scripts/validate.py
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
INTEG = ROOT / "custom_components" / "vigiora"
errors: list[str] = []


def paths(d: object, prefix: str = "") -> set[str]:
    if isinstance(d, dict):
        out: set[str] = set()
        for k, v in d.items():
            out |= paths(v, f"{prefix}.{k}" if prefix else k)
        return out
    return {prefix}


def flat(d: object, prefix: str = "") -> dict[str, str]:
    if isinstance(d, dict):
        out: dict[str, str] = {}
        for k, v in d.items():
            out.update(flat(v, f"{prefix}.{k}" if prefix else k))
        return out
    return {prefix: str(d)}


manifest = json.loads((INTEG / "manifest.json").read_text())
for key in (
    "domain",
    "name",
    "codeowners",
    "config_flow",
    "documentation",
    "iot_class",
    "issue_tracker",
    "requirements",
    "version",
    "integration_type",
):
    if key not in manifest:
        errors.append(f"manifest: missing {key}")
if manifest.get("domain") != INTEG.name:
    errors.append("manifest: domain does not match the folder")
hacs = json.loads((ROOT / "hacs.json").read_text())
if "name" not in hacs:
    errors.append("hacs.json: missing name")

strings = json.loads((INTEG / "strings.json").read_text())
services = yaml.safe_load((INTEG / "services.yaml").read_text())
for name, spec in services.items():
    s = strings.get("services", {}).get(name)
    if not s:
        errors.append(f"strings: service {name} missing")
        continue
    for field in (spec or {}).get("fields", {}):
        if field not in s.get("fields", {}):
            errors.append(f"strings: service {name} field {field} missing")
icons = json.loads((INTEG / "icons.json").read_text())
for name in services:
    if name not in icons.get("services", {}):
        errors.append(f"icons: service {name} missing")

ref_paths = paths(strings)
ref_flat = flat(strings)
ph = re.compile(r"\{[a-z_]+\}")
for tr in sorted((INTEG / "translations").glob("*.json")):
    data = json.loads(tr.read_text())
    if paths(data) != ref_paths:
        errors.append(f"{tr.name}: key structure differs from strings.json")
        continue
    for k, v in flat(data).items():
        if set(ph.findall(v)) != set(ph.findall(ref_flat[k])):
            errors.append(f"{tr.name}: placeholders differ at {k}")
if json.loads((INTEG / "translations" / "en.json").read_text()) != strings:
    errors.append("translations/en.json differs from strings.json")
for icon in ("icon.png", "icon@2x.png"):
    if not (INTEG / "brand" / icon).exists():
        errors.append(f"brand/{icon} missing")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print("structure checks passed")
