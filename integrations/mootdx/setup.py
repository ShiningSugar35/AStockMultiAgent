"""Rebuild the single project-owned Mootdx SDK environment from a hashed lock."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "integrations" / "mootdx"
ENVIRONMENT = ROOT / "runtime" / "tool-environments" / "mootdx"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-lock", action="store_true")
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to rebuild the isolated SDK")
    if not ENVIRONMENT.resolve().is_relative_to(ROOT):
        raise SystemExit("SDK environment must remain project-local")
    cache = ROOT / ".ai-bridge" / "tool-environments" / "mootdx" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".scratchpad"
    scratch.mkdir(exist_ok=True)
    lock = SOURCE / "requirements.lock"
    python = ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    with tempfile.TemporaryDirectory(prefix="mootdx-setup-", dir=scratch) as temporary:
        environment = os.environ.copy()
        environment.update({key: temporary for key in ("TMP", "TEMP", "TMPDIR")})
        environment.update({
            "UV_CACHE_DIR": str(cache), "PYTHONDONTWRITEBYTECODE": "1",
            "UV_PYTHON_DOWNLOADS": "never", "PYTHONIOENCODING": "utf-8",
        })

        def run(arguments: list[str]) -> None:
            subprocess.run([uv, *arguments], cwd=ROOT, env=environment, check=True)

        if args.refresh_lock or not lock.is_file():
            run(["pip", "compile", str(SOURCE / "requirements.in"),
                 "--python-version", "3.12", "--generate-hashes",
                 "--only-binary", ":all:", "--output-file", str(lock)])
        if not python.is_file():
            run(["venv", str(ENVIRONMENT), "--python", sys.executable])
        run(["pip", "sync", "--python", str(python), "--require-hashes", str(lock)])
    print("Mootdx isolated environment ready: runtime/tool-environments/mootdx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
