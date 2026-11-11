from pathlib import Path

from mrh.capture.strace import iter_syscalls, parse_exec_args, parse_strace, unescape

FIX = Path(__file__).parent / "fixtures"


def test_three_pid_prefix_forms_and_epoch_time():
    lines = [
        '1793000000.100000 execve("/bin/true", ["true"], 0x0 /* 0 vars */) = 0',
        '[pid  42] 1793000000.200000 execve("/bin/true", ["true"], 0x0) = 0',
        '43 1793000000.300000 execve("/bin/true", ["true"], 0x0) = 0',
    ]
    calls = list(iter_syscalls(lines))
    assert [c.pid for c in calls] == [None, 42, 43]
    assert calls[2].ts == 1793000000.3


def test_unfinished_resumed_stitched():
    lines = [
        '7 1.000000 clone3({flags=CLONE_VM}, 88 <unfinished ...>',
        '8 1.100000 execve("/bin/sh", ["sh"], 0x0) = 0',
        '7 1.200000 <... clone3 resumed>) = 8',
    ]
    calls = list(iter_syscalls(lines))
    clone = next(c for c in calls if c.call == "clone3")
    assert clone.ret == 8 and clone.ts == 1.0 and not clone.orphan


def test_orphan_resume_counted():
    p = parse_strace(['9 1.0 <... execve resumed>) = 0'])
    assert p.orphans == 1


def test_escapes_and_truncation():
    assert unescape(r"a\nb\x41\101\"") == 'a\nbAA"'
    binary, argv, trunc = parse_exec_args("execve", r'"/bin/bash", ["bash", "-c", "echo hi"...], 0x0')
    assert binary == "/bin/bash" and argv[-1] == "echo hi" and trunc


def test_process_tree_from_fixture():
    with (FIX / "executed" / "strace.log").open() as f:
        p = parse_strace(f)
    assert p.parents == {200: 100, 201: 200, 202: 200}
    assert p.ancestors(201) == [200, 100]
    cat = next(e for e in p.execs if e.argv[:1] == ["cat"])
    assert cat.ppid == 200
    writes = [f for f in p.files if f.mode == "write"]
    assert [w.path for w in writes] == ["/tmp/s"]


def test_connect_parsed():
    with (FIX / "refused" / "strace.log").open() as f:
        p = parse_strace(f)
    assert p.net[0].address == "127.0.0.1" and p.net[0].port == 18800
