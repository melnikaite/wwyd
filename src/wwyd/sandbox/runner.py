"""Run untrusted analyzer code in CPython-for-WASI under wasmtime (SPEC §7.5, D18).

One :class:`Sandbox` per process. Each :meth:`Sandbox.run` gets a fresh store with its own
fuel, epoch deadline and memory cap, a single read-only preopen (the stdlib), no network,
``PYTHONHOME`` as the only environment variable, the input as in-memory stdin bytes, stdout
captured up to a cap and stderr discarded. Only an exception *type name* ever leaves the
guest; messages and tracebacks never do (they can contain input text, P1).
"""

from __future__ import annotations

import ctypes
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wwyd.sandbox.cache import load_module

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
HELPERS_PATH = Path(__file__).resolve().parent / "helpers" / "wwyd_helpers.py"

TIMEOUT_S = 10.0
FUEL = 5_000_000_000
MEMORY_BYTES = 256 << 20
MAX_OUTPUT = 1 << 20
EPOCH_TICK_S = 0.01
DEFAULT_CONCURRENCY = 4

_NAME = re.compile(r"^[A-Za-z_]\w{0,63}$", re.ASCII)
_ERR_PREFIX = "\x00wwyd-error:"

# Bootstrap passed as `-c`. argv[1] is the helper module source, argv[2] the analyzer
# source: both live in guest memory, nothing is written to disk. The analyzer's stdout is
# buffered so that an exception discards partial output; on failure only the exception type
# name is written, behind a prefix no JSON document can start with.
_DRIVER = r"""
import sys, io, types
def _wwyd_main():
    helpers = types.ModuleType("wwyd_helpers")
    helpers.__file__ = "<wwyd_helpers>"
    exec(compile(sys.argv[1], "<wwyd_helpers>", "exec"), helpers.__dict__)
    sys.modules["wwyd_helpers"] = helpers
    src = sys.argv[2]
    del sys.argv[1:]
    real, buf = sys.stdout, io.StringIO()
    sys.stdout = buf
    try:
        exec(compile(src, "<analyzer>", "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        if e.code not in (None, 0):
            real.write("%PREFIX%SystemExit"); real.flush(); return
    except BaseException as e:
        real.write("%PREFIX%" + type(e).__name__); real.flush(); return
    real.write(buf.getvalue()); real.flush()
_wwyd_main()
""".replace("%PREFIX%", _ERR_PREFIX.encode("unicode_escape").decode())


@dataclass(frozen=True)
class Limits:
    timeout_s: float = TIMEOUT_S
    fuel: int = FUEL
    memory_bytes: int = MEMORY_BYTES
    max_output: int = MAX_OUTPUT


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class RunResult:
    """``output`` is the parsed JSON on success; ``error`` is a short code otherwise.

    Error codes: ``timeout``, ``out_of_fuel``, ``trap``, ``output_too_large``,
    ``invalid_output``, ``exit:<n>``, ``exception:<TypeName>``. ``wall_s`` is charged to
    ``sandbox_cpu_seconds_per_day`` by the caller.
    """

    output: Any | None
    error: str | None
    wall_s: float

    @property
    def ok(self) -> bool:
        return self.error is None


def assets_present(assets_dir: Path = ASSETS_DIR) -> bool:
    return (assets_dir / "python.wasm").is_file() and (
        assets_dir / "lib" / "python315.zip"
    ).is_file()


def helpers_source() -> str:
    return HELPERS_PATH.read_text(encoding="utf-8")


def _set_stdin_bytes(wasi: Any, payload: bytes) -> None:
    # wasmtime-py 49 has no public setter for in-memory stdin; the C API does.
    from wasmtime import _ffi as ffi

    vec = ffi.wasm_byte_vec_t()
    buf = ctypes.create_string_buffer(payload, len(payload))
    ffi.wasm_byte_vec_new(
        ctypes.byref(vec), len(payload), ctypes.cast(buf, ctypes.POINTER(ffi.wasm_byte_t))
    )
    ffi.wasi_config_set_stdin_bytes(wasi.ptr(), ctypes.byref(vec))  # takes ownership of vec


class Sandbox:
    """Process-wide sandbox runtime. Thread-safe; at most ``concurrency`` runs at a time."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        assets_dir: Path = ASSETS_DIR,
    ) -> None:
        import wasmtime

        if not assets_present(assets_dir):
            raise FileNotFoundError("sandbox assets are not built")
        self._wasmtime = wasmtime
        cfg = wasmtime.Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = True
        self.engine = wasmtime.Engine(cfg)
        engine_key = "fuel=1;epoch=1"
        t = time.perf_counter()
        self.module, self.cache_hit = load_module(
            self.engine, assets_dir / "python.wasm", cache_dir, engine_key
        )
        self.load_s = time.perf_counter() - t
        self.linker = wasmtime.Linker(self.engine)
        self.linker.define_wasi()
        self._lib = str(assets_dir / "lib")
        self._helpers = helpers_source()
        self._sem = threading.BoundedSemaphore(max(1, concurrency))
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="wwyd-sandbox-epoch", daemon=True)
        self._ticker.start()

    def _tick(self) -> None:
        while not self._stop.wait(EPOCH_TICK_S):
            self.engine.increment_epoch()

    def close(self) -> None:
        self._stop.set()

    def run(self, source: str, stdin: bytes = b"", limits: Limits = DEFAULT_LIMITS) -> RunResult:
        """Run ``source`` as ``__main__`` with ``stdin``; its stdout must be one JSON value."""
        with self._sem:
            t = time.perf_counter()
            error, out = self._run(source, stdin, limits)
            wall = time.perf_counter() - t
        if error is None:
            if out.startswith(_ERR_PREFIX.encode()):
                name = out[len(_ERR_PREFIX) :].decode("ascii", "replace")
                error = "exception:" + (name if _NAME.match(name) else "unknown")
            else:
                try:
                    return RunResult(json.loads(out), None, wall)
                except ValueError:
                    error = "invalid_output"
        return RunResult(None, error, wall)

    def _run(self, source: str, stdin: bytes, limits: Limits) -> tuple[str | None, bytes]:
        wt = self._wasmtime
        wasi = wt.WasiConfig()
        wasi.argv = ["python", "-S", "-B", "-s", "-c", _DRIVER, self._helpers, source]
        wasi.env = [("PYTHONHOME", "/")]
        wasi.preopen_dir(self._lib, "/lib", fs_mutable=False)
        _set_stdin_bytes(wasi, stdin)
        out = bytearray()
        over = False

        def on_out(b: bytes) -> int | None:
            # Must never raise: an exception here would abort the host call.
            nonlocal over
            if over or len(out) + len(b) > limits.max_output:
                over = True
                return -5  # EIO: the guest's write fails
            out.extend(b)
            return None

        def on_err(b: bytes) -> None:  # discarded: tracebacks may contain input (P1)
            return None

        wasi.stdout_custom = on_out
        wasi.stderr_custom = on_err
        store = wt.Store(self.engine)
        store.set_limits(memory_size=limits.memory_bytes, instances=2, tables=4, memories=1)
        store.set_fuel(limits.fuel)
        store.set_epoch_deadline(max(1, int(limits.timeout_s / EPOCH_TICK_S)))
        store.set_wasi(wasi)
        try:
            inst = self.linker.instantiate(store, self.module)
            inst.exports(store)["_start"](store)
        except wt.ExitTrap as e:
            if e.code != 0:
                return ("output_too_large" if over else f"exit:{int(e.code)}"), b""
        except wt.Trap as e:
            code = e.trap_code
            if code == wt.TrapCode.INTERRUPT:
                return "timeout", b""
            if code == wt.TrapCode.OUT_OF_FUEL:
                return "out_of_fuel", b""
            return "trap", b""
        except wt.WasmtimeError:
            return "trap", b""
        if over:
            return "output_too_large", b""
        return None, bytes(out)


_lock = threading.Lock()
_instance: Sandbox | None = None


def available(assets_dir: Path = ASSETS_DIR) -> bool:
    """True when the assets are built; callers skip LLM analyzers otherwise."""
    return assets_present(assets_dir)


def get_sandbox(cache_dir: Path | None = None, concurrency: int = DEFAULT_CONCURRENCY) -> Sandbox:
    """The process-wide sandbox, created on first use (the first call compiles or loads)."""
    global _instance
    with _lock:
        if _instance is None:
            if cache_dir is None:
                from wwyd.config import data_dir

                cache_dir = data_dir() / "cache"
            _instance = Sandbox(cache_dir, concurrency=concurrency)
        return _instance


def run(source: str, stdin: bytes = b"", limits: Limits = DEFAULT_LIMITS) -> RunResult:
    return get_sandbox().run(source, stdin, limits)
