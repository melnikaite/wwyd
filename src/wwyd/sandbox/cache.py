"""Compiled-module cache for the sandbox (SPEC §7.5).

Compiling ``python.wasm`` takes ~0.7 s; deserializing a cached module takes milliseconds.
The cache file lives in the data dir and is keyed by (wasmtime version, engine config, wasm
sha256), so an upgrade of either never loads a stale artifact. A cache file that fails to
load is replaced. The data dir is owner-controlled (mode 0700): a cached module is native
code, and nothing else ever writes there.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any


def cache_key(wasm_sha256: str, engine_key: str) -> str:
    raw = f"wasmtime={version('wasmtime')};{engine_key};wasm={wasm_sha256}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def load_module(
    engine: Any, wasm_path: Path, cache_dir: Path | None, engine_key: str
) -> tuple[Any, bool]:
    """Return ``(module, cache_hit)``; compiles and stores the module on a miss."""
    import wasmtime

    data = wasm_path.read_bytes()
    if cache_dir is None:
        return wasmtime.Module(engine, data), False
    path = cache_dir / f"python-{cache_key(hashlib.sha256(data).hexdigest(), engine_key)}.cwasm"
    if path.is_file():
        try:
            return wasmtime.Module.deserialize_file(engine, str(path)), True
        except wasmtime.WasmtimeError:
            pass
    module = wasmtime.Module(engine, data)
    try:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".python-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(module.serialize())
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only data dir only costs the compile on each start
    return module, False
