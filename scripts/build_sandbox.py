"""Build the sandbox assets (SPEC §7.5, §14) into ``src/wwyd/sandbox/assets/``.

Fetches (or takes with ``--zip``) the upstream CPython-for-WASI archive, verifies its
sha256 pin, strips the custom sections from ``python.wasm``, compiles a stdlib subset to
guest-compiled sourceless ``.pyc`` and packs it as ``lib/python315.zip`` (ZIP_STORED: the
wasm has no zlib), then writes ``manifest.json``. The output is deterministic.

The archive is untrusted data: it is only read as bytes here, and the interpreter inside it
only ever runs under wasmtime with the extracted stdlib preopened read-only and one
writable scratch directory. Run with ``uv run python -I scripts/build_sandbox.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from importlib.metadata import version
from pathlib import Path, PurePosixPath

UPSTREAM_TAG = "v3.15.0"
UPSTREAM_ASSET = "python-3.15.0-wasi_sdk-33.zip"
UPSTREAM_URL = (
    f"https://github.com/brettcannon/cpython-wasi-build/releases/download/"
    f"{UPSTREAM_TAG}/{UPSTREAM_ASSET}"
)
# Matches the asset digest GitHub reports for the upstream release (API field `digest`);
# update both lines together when moving to a new release.
UPSTREAM_SHA256 = "00b791b3729230f97a625087f534d71978433fb1e068e13a9cac8cd3f2541742"
PY_VER = "3.15"
STDLIB_PREFIX = f"lib/python{PY_VER}/"
ZIP_NAME = f"python{PY_VER.replace('.', '')}.zip"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)
MAX_ARCHIVE = 64 << 20

# Modules an analyzer may import, plus what they pull in. `decimal` resolves to
# `_pydecimal` (no `_decimal` in the build), so both are listed. No zoneinfo: the host does
# timezones (SPEC §7.5).
IMPORTS = (
    "json re collections statistics math datetime unicodedata itertools functools string "
    "decimal _pydecimal fractions heapq bisect operator random textwrap dataclasses enum "
    "typing types copy traceback encodings.ascii encodings.latin_1 encodings.utf_8"
)

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src" / "wwyd" / "sandbox" / "assets"

LIST_CODE = f"""
import sys, json, importlib, importlib.util
from importlib.machinery import PathFinder
for m in {IMPORTS!r}.split():
    importlib.import_module(m)
fs = set()
for n, mod in list(sys.modules.items()):
    fs.add(getattr(mod, "__file__", None) or "")
    # Look names up on the path too: some modules alias others in sys.modules
    # (decimal -> _pydecimal), and the alias's own file is still needed.
    try:
        sp = PathFinder.find_spec(n) if "." not in n else importlib.util.find_spec(n)
    except Exception:
        sp = None
    if sp and sp.origin:
        fs.add(sp.origin)
print(json.dumps(sorted(f for f in fs if f.startswith("/lib/python{PY_VER}/") and f.endswith(".py"))))
"""

COMPILE_CODE = """
import json, os, marshal, importlib.util, sys
files = json.loads(sys.argv[1])
for f in files:
    rel = f[len("/lib/python{v}/"):]
    dst = "/stage/" + rel[:-3] + ".pyc"
    d = "/stage"
    for part in rel.split("/")[:-1]:
        d += "/" + part
        if not os.path.isdir(d):
            os.mkdir(d)
    with open(f, "rb") as fh:
        code = compile(fh.read(), rel, "exec", optimize=2, dont_inherit=True)
    with open(dst, "wb") as fh:
        fh.write(importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little") + bytes(8)
                 + marshal.dumps(code))
""".replace("{v}", PY_VER)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _leb(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def strip_custom_sections(data: bytes) -> bytes:
    """Drop custom sections (id 0: names, debug info); everything else is kept verbatim."""
    if data[:4] != b"\0asm":
        raise SystemExit("python.wasm is not a wasm module")
    keep, i = [data[:8]], 8
    while i < len(data):
        start, sid = i, data[i]
        n, i = _leb(data, i + 1)
        end = i + n
        if end > len(data):
            raise SystemExit("truncated wasm section")
        if sid != 0:
            keep.append(data[start:end])
        i = end
    return b"".join(keep)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (fixed https URL)
        data = resp.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise SystemExit("archive too large")
    return data


def extract(archive: bytes, dest: Path) -> bytes:
    """Write the stdlib sources under dest/lib; return python.wasm bytes."""
    wasm = None
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for info in z.infolist():
            p = PurePosixPath(info.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                raise SystemExit(f"unsafe path in archive: {info.filename!r}")
            if info.filename == "python.wasm":
                wasm = z.read(info)
            elif info.filename.startswith(STDLIB_PREFIX) and not info.is_dir():
                target = dest / p
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    if wasm is None:
        raise SystemExit("python.wasm missing from archive")
    return wasm


def run_guest(engine, module, lib: Path, argv: list[str], stage: Path | None) -> bytes:
    import wasmtime

    wasi = wasmtime.WasiConfig()
    wasi.argv = argv
    wasi.env = [("PYTHONHOME", "/")]
    wasi.preopen_dir(str(lib), "/lib", fs_mutable=False)
    if stage is not None:
        wasi.preopen_dir(str(stage), "/stage")
    out = bytearray()

    def on_out(b: bytes) -> None:
        out.extend(b)

    wasi.stdout_custom = on_out
    wasi.inherit_stderr()
    store = wasmtime.Store(engine)
    store.set_wasi(wasi)
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    try:
        linker.instantiate(store, module).exports(store)["_start"](store)
    except wasmtime.ExitTrap as e:
        if e.code != 0:
            raise SystemExit(f"guest build step exited with {e.code}") from None
    return bytes(out)


def build(archive: bytes, out: Path) -> dict:
    import wasmtime

    digest = sha256(archive)
    if digest != UPSTREAM_SHA256:
        raise SystemExit(f"sha256 mismatch: got {digest}, pinned {UPSTREAM_SHA256}")
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        wasm = strip_custom_sections(extract(archive, tmpd / "src"))
        lib = tmpd / "src" / "lib"
        stage = tmpd / "stage"
        stage.mkdir()
        engine = wasmtime.Engine()
        module = wasmtime.Module(engine, wasm)
        argv0 = ["python", "-S", "-B", "-s", "-c"]
        files = json.loads(run_guest(engine, module, lib, [*argv0, LIST_CODE], None))
        run_guest(engine, module, lib, [*argv0, COMPILE_CODE, json.dumps(files)], stage)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for p in sorted(stage.rglob("*.pyc")):
                info = zipfile.ZipInfo(p.relative_to(stage).as_posix(), ZIP_DATE)
                info.external_attr = 0o644 << 16
                z.writestr(info, p.read_bytes())
        stdlib_zip = buf.getvalue()

    if out.exists():
        shutil.rmtree(out)
    (out / "lib").mkdir(parents=True)
    (out / "python.wasm").write_bytes(wasm)
    (out / "lib" / ZIP_NAME).write_bytes(stdlib_zip)
    manifest = {
        "upstream": {"url": UPSTREAM_URL, "sha256": digest},
        "python": PY_VER,
        "built_with_wasmtime": version("wasmtime"),
        "python_wasm_sha256": sha256(wasm),
        "stdlib_zip_sha256": sha256(stdlib_zip),
        "stdlib_modules": len(files),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", type=Path, help="use a local copy of the upstream archive")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = ap.parse_args(argv)
    archive = args.zip.read_bytes() if args.zip else fetch(UPSTREAM_URL)
    m = build(archive, args.out)
    wasm_size = (args.out / "python.wasm").stat().st_size
    zip_size = (args.out / "lib" / ZIP_NAME).stat().st_size
    print(f"{m['stdlib_modules']} modules; python.wasm {wasm_size} B, {ZIP_NAME} {zip_size} B")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
