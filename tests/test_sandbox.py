"""Sandbox runner and isolation (SPEC §7.5, §13). Skipped when the assets are not built
(build them with ``uv run python -I scripts/build_sandbox.py``)."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from wwyd.sandbox import runner
from wwyd.sandbox.helpers import wwyd_helpers as h
from wwyd.sandbox.runner import Limits, Sandbox

needs_assets = pytest.mark.skipif(not runner.available(), reason="sandbox assets not built")

SECRET = "SECRET_INPUT_TEXT_42"
STDIN = json.dumps([{"said": SECRET, "month": "2026-02"}]).encode()
FAST = Limits(timeout_s=5, fuel=2_000_000_000)


@pytest.fixture(scope="module")
def sb(tmp_path_factory):
    s = Sandbox(tmp_path_factory.mktemp("sbcache"))
    yield s
    s.close()


def _assert_no_secret(r):
    assert SECRET not in repr(r)


# --- helpers (host side, no assets needed) ---


def test_helpers_native():
    toks = h.tokenize("Hello wORLD don't")
    assert toks == ["hello", "world", "don't"]
    assert [t.case for t in toks] == ["title", "mixed", "lower"]
    assert h.words("Привет, мир 42!") == ["привет", "мир"]
    assert h.sentences("One. Two! Three? four") == ["One.", "Two!", "Three?", "four"]
    assert h.emojis("ok 👍🏽 🙂") == ["👍", "🙂"]
    assert [h.ending(s) for s in ["a.", "a!", "a?", "a…", "a...", "a)))", "(a)", "a", "a 🙂", ""]] == [
        "period", "exclamation", "question", "ellipsis", "ellipsis", "smiley", "other", "none",
        "emoji", "none",
    ]  # fmt: skip
    assert h.count_bigrams(["a b c", "a b"]) == {"a b": 2, "b c": 1}
    assert h.month_key("2026-02-03T10:00:00+01:00") == "2026-02"
    with pytest.raises(ValueError):
        h.month_key("2026-13-01")
    assert h.histogram([0, 1, 5, 99], [1, 5, 10]) == {"<1": 1, "1-5": 1, "5-10": 1, "10+": 1}
    assert h.add_counts({}, "2026-01", "m", "k", 2) == {"m": {"2026-01": {"k": 2}}}


def test_available_false_without_assets(tmp_path):
    assert not runner.available(tmp_path)
    with pytest.raises(FileNotFoundError):
        Sandbox(tmp_path, assets_dir=tmp_path)


# --- runner ---


@needs_assets
def test_basic_json_and_wall_time(sb):
    src = "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps({'n': len(d)}))"
    r = sb.run(src, STDIN)
    assert r.ok and r.output == {"n": 1}
    assert 0 < r.wall_s < 5


@needs_assets
def test_helpers_in_guest_match_host(sb):
    text = "Привет, как дела)))  ok 🙂 Hi hi."
    src = (
        "import sys, json, wwyd_helpers as h\n"
        "t = json.load(sys.stdin)\n"
        "print(json.dumps([h.words(t), h.sentences(t), h.emojis(t), h.ending(t),"
        " dict(h.count_bigrams([t])), [x.case for x in h.tokenize(t)]]))"
    )
    r = sb.run(src, json.dumps(text).encode())
    assert r.ok, r.error
    expected = [h.words(text), h.sentences(text), h.emojis(text), h.ending(text),
                dict(h.count_bigrams([text])), [x.case for x in h.tokenize(text)]]  # fmt: skip
    assert r.output == expected


@needs_assets
def test_module_cache_reused(tmp_path):
    a = Sandbox(tmp_path)
    b = Sandbox(tmp_path)
    try:
        assert not a.cache_hit and b.cache_hit
        assert b.load_s < a.load_s
        assert len(list(tmp_path.glob("python-*.cwasm"))) == 1
        assert b.run("print(1)").output == 1
    finally:
        a.close()
        b.close()


@needs_assets
def test_corrupt_cache_recompiles(tmp_path):
    Sandbox(tmp_path).close()
    (f,) = tmp_path.glob("python-*.cwasm")
    f.write_bytes(b"garbage")
    s = Sandbox(tmp_path)
    try:
        assert not s.cache_hit and s.run("print(2)").output == 2
    finally:
        s.close()


@needs_assets
def test_concurrency_limit(tmp_path):
    s = Sandbox(None, concurrency=2)
    active = peak = 0
    lock = threading.Lock()
    inner = s._run

    def tracked(*a):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            return inner(*a)
        finally:
            with lock:
                active -= 1

    s._run = tracked
    try:
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(lambda _: s.run("print(sum(range(300000)))"), range(6)))
        assert all(r.output == sum(range(300000)) for r in res)
        assert peak == 2
        # a hostile run does not disturb the others
        with ThreadPoolExecutor(3) as ex:
            bad = ex.submit(s.run, "while True: pass", b"", Limits(timeout_s=0.5, fuel=10**15))
            good = [ex.submit(s.run, "print(3)") for _ in range(2)]
            assert [g.result().output for g in good] == [3, 3]
            assert bad.result().error == "timeout"
    finally:
        s.close()


# --- isolation: every probe must fail closed and never leak input text ---

PROBES = {
    "read_etc_passwd": "print(open('/etc/passwd').read())",
    "list_root": "import os\nprint(os.listdir('/'))",
    "list_home": f"import os\nprint(os.listdir({os.path.expanduser('~')!r}))",
    "write_lib": "open('/lib/pwn.txt', 'w').write('x')",
    "write_cwd": "open('pwn.txt', 'w').write('x')",
    "escape_dotdot": "print(open('/lib/../../etc/hosts').read())",
    "socket": "import socket\nsocket.create_connection(('1.1.1.1', 80))",
    "_socket": "import _socket\n_socket.socket().connect(('1.1.1.1', 80))",
    "socket_fd3": "import _socket\n_socket.socket(fileno=3)",
    "subprocess": "import subprocess\nsubprocess.run(['id'])",
    "os_system": "import os\nos.system('id')",
    "os_exec": "import os\nos.execv('/bin/sh', ['sh'])",
    "ctypes": "import ctypes",
    "raise_with_input": "import sys\nraise ValueError(sys.stdin.read())",
    "deep_recursion": "def f(n): return f(n + 1)\nf(0)",
    "sys_exit": "import sys\nsys.exit(3)",
}


@needs_assets
@pytest.mark.parametrize("name", sorted(PROBES))
def test_isolation_probe(sb, name):
    r = sb.run(PROBES[name], STDIN, FAST)
    assert r.error is not None and r.output is None
    assert r.error.startswith(("exception:", "exit:", "trap"))
    _assert_no_secret(r)


@needs_assets
def test_nothing_written_to_assets(sb):
    sb.run(PROBES["write_lib"], b"", FAST)
    assert sorted(p.name for p in (runner.ASSETS_DIR / "lib").iterdir()) == ["python315.zip"]


@needs_assets
def test_env_only_pythonhome(sb):
    r = sb.run("import os, json\nprint(json.dumps(dict(os.environ)))")
    assert r.output == {"PYTHONHOME": "/"}
    r = sb.run("import sys, json\nprint(json.dumps(sys.argv))")
    assert r.output == ["-c"]


@needs_assets
def test_stdout_before_exception_is_discarded(sb):
    r = sb.run("import sys\nprint(sys.stdin.read())\nraise KeyError", STDIN, FAST)
    assert r.error == "exception:KeyError"
    _assert_no_secret(r)


@needs_assets
def test_infinite_loop_stopped_by_fuel(sb):
    r = sb.run("while True: pass", b"", Limits(timeout_s=60, fuel=200_000_000))
    assert r.error == "out_of_fuel" and r.wall_s < 30


@needs_assets
def test_infinite_loop_stopped_by_epoch(sb):
    r = sb.run("while True: pass", b"", Limits(timeout_s=0.5, fuel=10**15))
    assert r.error == "timeout" and r.wall_s < 3


@needs_assets
def test_loop_inside_c_code_stopped(sb):
    src = "x = 10 ** 10 ** 8"
    assert sb.run(src, b"", Limits(timeout_s=0.5, fuel=10**15)).error == "timeout"
    assert sb.run(src, b"", Limits(timeout_s=60, fuel=200_000_000)).error == "out_of_fuel"


@needs_assets
def test_memory_bomb(sb):
    src = "x = []\nwhile True: x.append(bytearray(10**7))"
    r = sb.run(src, b"", Limits(timeout_s=10, fuel=10**12, memory_bytes=64 << 20))
    assert r.error in ("exception:MemoryError", "trap") or r.error.startswith("exit:")


@needs_assets
def test_oversized_output(sb):
    r = sb.run("print('x' * (2 << 20))", b"", FAST)
    assert r.error == "output_too_large"
    r = sb.run("import sys\nsys.__stdout__.write('[' + '1,' * (1 << 20) + '1]')", b"", FAST)
    assert r.error == "output_too_large"


@needs_assets
def test_invalid_json_output(sb):
    r = sb.run("import sys\nprint(sys.stdin.read()[2:-2])", STDIN, FAST)
    assert r.error == "invalid_output"
    _assert_no_secret(r)


@needs_assets
def test_forged_exception_names(sb):
    prefix = runner._ERR_PREFIX.encode("unicode_escape").decode()
    forge = (
        "import sys, os\nsys.__stdout__.write('{p}' + {name}); sys.__stdout__.flush(); os._exit(0)"
    )
    r = sb.run(forge.format(p=prefix, name="sys.stdin.read()"), STDIN, FAST)
    assert r.error == "exception:unknown"
    _assert_no_secret(r)
    r = sb.run(forge.format(p=prefix, name="'Nice_Name1'"), b"", FAST)
    assert r.error == "exception:Nice_Name1"
    r = sb.run(f"class {'E' * 80}(Exception): pass\nraise {'E' * 80}", b"", FAST)
    assert r.error == "exception:unknown"


@needs_assets
def test_trivial_run_is_fast(sb):
    sb.run("print(0)")
    t = time.perf_counter()
    for _ in range(5):
        assert sb.run("print(0)").output == 0
    assert (time.perf_counter() - t) / 5 < 1.0
