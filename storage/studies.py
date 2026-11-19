"""Study archive: one folder per study, the files are the record.

    <data>/studies/<id>/study.json          the study: settings, steps, positions, results
                       /profile.toml        the position profile as it was when recording began
                       /masks/<pos>.json    each position's masks, frozen at the same moment
                       /brackets/...        raw brackets: sweep_NN, NN_<pos>, check_<pos>
                       /results/...         per-bracket processing output (result.json, heatmap)
                       /background.json     L_b from the hemisphere sweep

Profiles and masks are copied in, not referenced: editing a profile afterwards must not change
what a past study says it measured. A study can always be re-analysed from its own folder.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class StudyNotFound(KeyError):
    def __str__(self) -> str:   # KeyError quotes its message; this one is for people
        return str(self.args[0]) if self.args else "no such study"


class StudyStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def new_id(self, profile_slug: str) -> str:
        base = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{profile_slug}"[:110]
        sid, n = base, 1
        while (self.root / sid).exists():
            n += 1
            sid = f"{base}-{n}"
        return sid

    def path(self, study_id: str) -> Path:
        if not ID_RE.match(study_id):
            raise StudyNotFound(f"no study {study_id!r}")
        return self.root / study_id

    def create(self, study_id: str) -> Path:
        d = self.path(study_id)
        d.mkdir(parents=True, exist_ok=False)
        return d

    def save(self, study: dict[str, Any]) -> None:
        d = self.path(study["id"])
        tmp = d / "study.json.tmp"
        tmp.write_text(json.dumps(study, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
        os.replace(tmp, d / "study.json")

    def load(self, study_id: str) -> dict[str, Any]:
        f = self.path(study_id) / "study.json"
        if not f.is_file():
            raise StudyNotFound(f"no study {study_id!r}")
        return json.loads(f.read_text(encoding="utf-8"))

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted((p.name for p in self.root.iterdir()
                       if (p / "study.json").is_file()), reverse=True)

    def file(self, study_id: str, rel: str) -> Path:
        """A file inside the study folder; anything that resolves outside it is refused."""
        base = self.path(study_id).resolve()
        target = (base / rel).resolve()
        if base not in target.parents or not target.is_file():
            raise StudyNotFound(f"{study_id}: no file {rel!r}")
        return target

    def results_zip(self, study_id: str) -> bytes:
        """Everything but the raw brackets and the luminance arrays: what a report needs.
        A few MB per study, so it is built in memory."""
        base = self.path(study_id)
        if not (base / "study.json").is_file():
            raise StudyNotFound(f"no study {study_id!r}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(base.rglob("*")):
                rel = f.relative_to(base)
                if not f.is_file() or rel.parts[0] == "brackets" or f.suffix in (".npy", ".tmp"):
                    continue
                z.write(f, f"{study_id}/{rel.as_posix()}")
        return buf.getvalue()
