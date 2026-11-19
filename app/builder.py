"""Core build logic: dependency-aware object caching with singleflight.

Cache key design
----------------
For each translation unit (.c file) the cache key is the SHA-256 of:

* the fixed GCC flags,
* the source file's project-relative path and content,
* the path and content of every *project* header transitively reachable
  from that source via #include directives (the dependency closure).

Include resolution mirrors GCC: quoted includes are searched relative to
the directory of the file containing the directive first, then the project
root (which is passed to GCC with -I). Angle includes are searched in the
project root only. Includes that do not resolve to a submitted project file
are treated as system headers and do not participate in the key.

Because resolution is part of the key, adding a same-named header that
shadows another one changes the dependency closure of the affected units
and therefore their keys — those units are recompiled while unaffected
units keep hitting the cache.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Fixed GCC parameters used for every compile and link. Part of the cache key.
CFLAGS = ("-O2", "-Wall", "-Wextra", "-std=c11")

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
COMPILE_TIMEOUT_S = 60
LINK_TIMEOUT_S = 60

_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]', re.MULTILINE)
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")


class BuildError(Exception):
    """A build failure whose diagnostics should be returned to the caller."""

    def __init__(self, stage: str, message: str, unit: str | None = None, diagnostics: str = ""):
        super().__init__(message)
        self.stage = stage  # "validation" | "compile" | "link"
        self.unit = unit  # source file that failed, if any
        self.diagnostics = diagnostics or message


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_path(path: str) -> str:
    p = path.strip()
    if not p or p != path:
        raise ValueError(f"invalid path {path!r}: empty or surrounding whitespace")
    if len(p) > 255:
        raise ValueError(f"invalid path {path!r}: too long")
    if p.startswith("/") or "\\" in p or "\x00" in p:
        raise ValueError(f"invalid path {path!r}: must be a relative POSIX path")
    for seg in p.split("/"):
        if seg in (".", "..") or not _PATH_SEGMENT_RE.fullmatch(seg):
            raise ValueError(f"invalid path {path!r}: bad segment {seg!r}")
    if not (p.endswith(".c") or p.endswith(".h")):
        raise ValueError(f"invalid path {path!r}: only .c and .h files are accepted")
    return p


def validate_request(files) -> dict[str, str]:
    """Validate the submitted file list, return {relative_path: content}."""
    if not files:
        raise BuildError("validation", "request contains no files")
    if len(files) > MAX_FILES:
        raise BuildError("validation", f"too many files (max {MAX_FILES})")
    by_path: dict[str, str] = {}
    errors: list[str] = []
    for f in files:
        try:
            p = validate_path(f.path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if p in by_path:
            errors.append(f"duplicate path {p!r}")
            continue
        if len(f.content.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            errors.append(f"file {p!r} exceeds {MAX_FILE_BYTES} bytes")
            continue
        by_path[p] = f.content
    if errors:
        raise BuildError("validation", "; ".join(errors))
    if not any(p.endswith(".c") for p in by_path):
        raise BuildError("validation", "no .c source files submitted")
    return by_path


# ---------------------------------------------------------------------------
# Dependency closure and cache key
# ---------------------------------------------------------------------------

def _resolve_include(name: str, quoted: bool, including_dir: str,
                     files: dict[str, str]) -> str | None:
    candidates = []
    if quoted:
        # GCC searches the directory of the file containing the directive first.
        candidates.append(posixpath.normpath(posixpath.join(including_dir, name)))
    # Then the include path: the project root (passed as -I<root>).
    candidates.append(posixpath.normpath(name))
    for cand in candidates:
        if cand.startswith("/") or cand == ".." or cand.startswith("../"):
            continue
        if cand in files:
            return cand
    return None  # system header or missing file: not part of the cache key


def dependency_closure(src: str, files: dict[str, str]) -> dict[str, str]:
    """All project headers transitively included by src: {path: content}."""
    closure: dict[str, str] = {}
    stack = [src]
    while stack:
        current = stack.pop()
        current_dir = posixpath.dirname(current)
        for m in _INCLUDE_RE.finditer(files[current]):
            target = _resolve_include(m.group(2), m.group(1) == '"', current_dir, files)
            if target is None or target in closure:
                continue
            closure[target] = files[target]
            stack.append(target)
    return closure


def unit_cache_key(src: str, files: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(b"cbuild-v1\0")
    for flag in CFLAGS:
        h.update(flag.encode())
        h.update(b"\0")
    h.update(src.encode())
    h.update(b"\0")
    h.update(files[src].encode())
    h.update(b"\0")
    for path, content in sorted(dependency_closure(src, files).items()):
        h.update(path.encode())
        h.update(b"\0")
        h.update(content.encode())
        h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Object cache with singleflight
# ---------------------------------------------------------------------------

@dataclass
class CacheStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests_total: int = 0
    failed_builds_total: int = 0
    compiled_total: int = 0
    reused_total: int = 0
    singleflight_waits_total: int = 0


class ObjectCache:
    """Content-addressed object-file cache with per-key singleflight.

    Identical compile inputs arriving concurrently are compiled exactly
    once; the other callers wait on the per-key lock and then read the
    result from the cache. Only successful compilations enter the cache.
    """

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats = CacheStats()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def get_or_compile(self, key: str, compile_fn) -> tuple[bytes, str]:
        """Return (object_bytes, status); status in compiled|reused|waited."""
        path = self.dir / f"{key}.o"
        if path.is_file():
            with self.stats.lock:
                self.stats.reused_total += 1
            return path.read_bytes(), "reused"
        lock = self._lock_for(key)
        with lock:
            if path.is_file():
                # A concurrent request compiled this very key while we waited.
                with self.stats.lock:
                    self.stats.reused_total += 1
                    self.stats.singleflight_waits_total += 1
                return path.read_bytes(), "waited"
            data = compile_fn()  # raises BuildError on failure: nothing is cached
            tmp = self.dir / f"{key}.{threading.get_ident()}.tmp"
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic publish
            with self.stats.lock:
                self.stats.compiled_total += 1
            return data, "compiled"

    def cached_objects(self) -> int:
        return sum(1 for _ in self.dir.glob("*.o"))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    executable: bytes
    compiled: int
    reused: int
    units: int


def build_project(files: dict[str, str], cache: ObjectCache, work_root: Path) -> BuildResult:
    """Compile every .c unit (reusing cached objects) and link this
    submission's objects only into one executable."""
    work_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="build-", dir=work_root))
    try:
        for rel, content in files.items():
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        sources = sorted(p for p in files if p.endswith(".c"))
        obj_dir = tmp / ".objs"
        obj_dir.mkdir()
        objects: list[Path] = []
        compiled = 0
        reused = 0

        for i, src in enumerate(sources):
            key = unit_cache_key(src, files)

            def _compile(src=src, key=key) -> bytes:
                out = obj_dir / f".tmp-{key}.o"
                cmd = ["gcc", *CFLAGS, f"-I{tmp}", "-c", str(tmp / src), "-o", str(out)]
                try:
                    proc = subprocess.run(cmd, cwd=tmp, capture_output=True,
                                          text=True, timeout=COMPILE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    raise BuildError("compile", f"compilation of {src} timed out", unit=src)
                if proc.returncode != 0:
                    raise BuildError("compile", f"compilation of {src} failed",
                                     unit=src, diagnostics=proc.stderr)
                return out.read_bytes()

            data, status = cache.get_or_compile(key, _compile)
            if status == "compiled":
                compiled += 1
            else:  # "reused" or "waited": this request did not compile it
                reused += 1
            obj = obj_dir / f"{i:04d}-{key[:16]}.o"
            obj.write_bytes(data)
            objects.append(obj)

        exe = tmp / "a.out"
        cmd = ["gcc", *CFLAGS, "-o", str(exe), *(str(o) for o in objects)]
        try:
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True,
                                  text=True, timeout=LINK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise BuildError("link", "linking timed out")
        if proc.returncode != 0:
            raise BuildError("link", "linking failed", diagnostics=proc.stderr)

        return BuildResult(executable=exe.read_bytes(), compiled=compiled,
                           reused=reused, units=len(sources))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
