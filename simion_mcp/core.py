"""SIMION install resolution, subprocess execution, and .gem geometry parsing."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT_SEC = 300

# Files that make up a self-contained workbench, for workspace copying.
WORKBENCH_GLOBS = ("*.iob", "*.gem", "*.fly", "*.fly2", "*.pa*", "*.lua", "*.rec")


def simion_home() -> Optional[Path]:
    raw = os.environ.get("SIMION_HOME")
    return Path(raw).resolve() if raw else None


def simion_exe() -> Optional[Path]:
    home = simion_home()
    return home / "simion.exe" if home else None


def check_install() -> Optional[str]:
    """Return an error string if SIMION isn't usable, else None."""
    if not os.environ.get("SIMION_HOME"):
        return "SIMION_HOME environment variable is not set."
    exe = simion_exe()
    if not exe or not exe.exists():
        return f"simion.exe not found at {exe}. Check SIMION_HOME."
    return None


def run_simion(
    args: list[str],
    cwd: Optional[Path] = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Run `simion.exe --nogui --noprompt <args>` and capture structured output.

    SIMION reports failures as `error,...` lines on stdout while still exiting 0,
    so success is judged on both the return code and the absence of those lines.
    """
    err = check_install()
    if err:
        return {"ok": False, "error": err}

    cmd = [str(simion_exe()), "--nogui", "--noprompt", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"SIMION timed out after {timeout_sec}s",
            "command": " ".join(cmd),
        }

    stdout = proc.stdout or ""
    error_lines = [ln for ln in stdout.splitlines() if ln.startswith("error,")]
    return {
        "ok": proc.returncode == 0 and not error_lines,
        "return_code": proc.returncode,
        "command": " ".join(cmd),
        "stdout": stdout,
        "stderr": proc.stderr or "",
        "error_lines": error_lines,
    }


def lua_assignments(variables: dict) -> list[str]:
    """Build `--lua NAME=VALUE` argument pairs, validating identifiers.

    Raises ValueError on a name that isn't a plain Lua identifier, so callers
    can't smuggle arbitrary code in through a variable name.
    """
    args: list[str] = []
    for name, value in (variables or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            raise ValueError(f"Invalid Lua variable name: {name!r}")
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = repr(value)
        else:
            escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
            literal = f"'{escaped}'"
        args += ["--lua", f"{name}={literal}"]
    return args


# --- .gem geometry parsing -------------------------------------------------
# Electrode names come from trailing comments on the declaration, the
# convention simPyon established: `electrode(1); collimator`.

_ELECTRODE_RE = re.compile(
    r"\b(?P<kind>electrode|e)\s*\(\s*(?P<num>\d+)\s*\)(?P<rest>[^\n]*)"
)
_INCLUDE_RE = re.compile(r"\binclude\s*\(\s*(?P<path>[^)]+?)\s*\)")
_PA_DEFINE_RE = re.compile(r"\bpa_define\s*\((?P<args>[^)]*)\)")


def _strip_block_comments(text: str) -> str:
    return re.sub(r"\{[^{}]*\}", lambda m: " " * len(m.group(0)), text)


def parse_gem(gem_path: Path, _seen: Optional[set] = None) -> dict:
    """Parse a .gem file into electrode numbers, names, and array metadata.

    Follows include() directives so multi-file geometries resolve fully.
    Electrode 0 is SIMION's implicit ground and is reported but flagged.
    """
    gem_path = Path(gem_path).resolve()
    _seen = _seen if _seen is not None else set()
    if gem_path in _seen:
        return {"electrodes": {}, "pa_define": None, "includes": []}
    _seen.add(gem_path)

    if not gem_path.exists():
        return {"error": f"gem file not found: {gem_path}"}

    text = gem_path.read_text(errors="replace")

    pa_define = None
    pm = _PA_DEFINE_RE.search(text)
    if pm:
        pa_define = pm.group("args").strip()

    electrodes: dict[int, dict] = {}
    for line in text.splitlines():
        # An electrode declaration's name is the trailing `; comment` on its
        # own line; strip any brace body first so nested geometry isn't scanned.
        scan = _strip_block_comments(line)
        for m in _ELECTRODE_RE.finditer(scan):
            num = int(m.group("num"))
            rest = line[m.end("rest") - len(m.group("rest")):]
            name = None
            if ";" in rest:
                candidate = rest.split(";", 1)[1].strip()
                # Ignore geometry that merely follows on the same line.
                if candidate and not candidate.startswith(("{", "}")):
                    name = candidate or None
            existing = electrodes.get(num)
            if existing is None:
                electrodes[num] = {"number": num, "name": name}
            elif existing.get("name") is None and name:
                existing["name"] = name

    includes = []
    for m in _INCLUDE_RE.finditer(text):
        inc_raw = m.group("path").strip().strip('"\'')
        inc_path = (gem_path.parent / inc_raw).resolve()
        includes.append(str(inc_path))
        sub = parse_gem(inc_path, _seen)
        for num, info in sub.get("electrodes", {}).items():
            existing = electrodes.get(num)
            if existing is None:
                electrodes[num] = info
            elif existing.get("name") is None and info.get("name"):
                existing["name"] = info["name"]
        if pa_define is None and sub.get("pa_define"):
            pa_define = sub["pa_define"]

    return {"electrodes": electrodes, "pa_define": pa_define, "includes": includes}


def copy_workbench(src_dir: Path, dest_dir: Path, overwrite: bool = False) -> dict:
    """Copy a workbench's files into a working directory.

    Copying rather than mutating in place matters because running a simulation
    with voltage control writes a workbench Lua program next to the .iob.
    """
    src_dir = Path(src_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    if not src_dir.is_dir():
        return {"ok": False, "error": f"Source directory not found: {src_dir}"}
    if dest_dir.exists() and any(dest_dir.iterdir()) and not overwrite:
        return {
            "ok": False,
            "error": f"Destination {dest_dir} is not empty. Pass overwrite=true to reuse it.",
        }
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for pattern in WORKBENCH_GLOBS:
        for f in src_dir.glob(pattern):
            if f.is_file():
                shutil.copy2(f, dest_dir / f.name)
                copied.append(f.name)
    if not copied:
        return {"ok": False, "error": f"No workbench files found in {src_dir}"}
    return {"ok": True, "work_dir": str(dest_dir), "copied": sorted(copied)}


def backup_file(path: Path) -> str:
    """Copy a file alongside itself with a timestamp suffix; return the backup path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return str(backup)
