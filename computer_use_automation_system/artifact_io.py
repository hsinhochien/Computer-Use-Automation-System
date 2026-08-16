from __future__ import annotations

import json
from pathlib import Path

import yaml

from .models import Artifact


def load_artifact(file_path: str | Path) -> Artifact:
    path = Path(file_path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw)
    return Artifact.model_validate(data)


def save_artifact(file_path: str | Path, artifact: Artifact) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = artifact.model_dump(mode="json")
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
