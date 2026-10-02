"""Tests for the measure_ram.py helper bundled with the measuring-replication-ram skill."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import textwrap
import time

import measure_ram
import pytest
from conftest import SCRIPTS_DIR

MIB = 1024**2
ALLOC_MIB = 200


def allocating_script(mib: int = ALLOC_MIB, hold_s: float = 1.5) -> str:
    """Python source that touches ``mib`` MiB of memory and holds it for ``hold_s`` seconds."""
    return f"import time; x = bytearray({mib} * 1024 * 1024); time.sleep({hold_s})"


@pytest.mark.parametrize(
    ("n_bytes", "expected"),
    [(512, "0 KiB"), (5 * 1024, "5 KiB"), (300 * MIB, "300 MiB"), (int(12.34 * 1024**3), "12.3 GiB")],
)
def test_format_bytes(n_bytes: int, expected: str) -> None:
    assert measure_ram.format_bytes(n_bytes) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(4.24, "4.2s"), (192, "3m 12s"), (6 * 3600 + 40 * 60 + 12, "6h 40m"), (9 * 3600 + 15 * 60, "9h 15m")],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert measure_ram.format_duration(seconds) == expected


def test_record_tracks_maxima_and_peak_time() -> None:
    m = measure_ram.Measurement(label="x", command="x")
    m.record(measure_ram.TreeSample(100, 1, 0), 0.0)
    m.record(measure_ram.TreeSample(500, 3, 0), 2.0)
    m.record(measure_ram.TreeSample(200, 2, 800), 4.0)
    assert m.sampled_tree_peak_bytes == 500
    assert m.peak_at_s == 2.0
    assert m.max_processes == 3
    assert m.os_single_process_peak_bytes == 800
    assert m.peak_bytes == 800
    assert m.n_samples == 3


def test_readme_row_has_all_columns() -> None:
    m = measure_ram.Measurement(
        label="Baseline model",
        command="stata-mp -b do main.do",
        peak_bytes=207 * 1024**3,
        wall_time_s=6 * 3600 + 40 * 60,
        machine={"os": "Linux 6.8", "arch": "x86_64", "logical_cpus": 32, "total_ram_bytes": 512 * 1024**3},
    )
    assert measure_ram.readme_row(m) == (
        "| Baseline model | 207.0 GiB | 6h 40m | Linux 6.8 x86_64, 32 logical CPUs, 512 GiB RAM |"
    )


def test_measure_command_captures_peak_and_writes_trace(tmp_path) -> None:
    trace = tmp_path / "logs" / "trace.csv"
    m = measure_ram.measure_command([sys.executable, "-c", allocating_script()], interval=0.1, trace_path=trace)

    assert m.exit_code == 0
    assert m.sampled_tree_peak_bytes >= 0.9 * ALLOC_MIB * MIB
    assert m.peak_bytes >= m.sampled_tree_peak_bytes
    assert m.wall_time_s >= 1.5
    rows = list(csv.reader(trace.open()))
    assert tuple(rows[0]) == measure_ram.TRACE_HEADER
    assert len(rows) - 1 == m.n_samples >= 5


def test_measure_command_includes_child_processes() -> None:
    parent = textwrap.dedent(
        f"""
        import subprocess, sys
        subprocess.run([sys.executable, "-c", {allocating_script()!r}], check=True)
        """
    )
    m = measure_ram.measure_command([sys.executable, "-c", parent], interval=0.1, label="parent+child")

    assert m.label == "parent+child"
    assert m.max_processes >= 2
    assert m.sampled_tree_peak_bytes >= 0.9 * ALLOC_MIB * MIB


def test_measure_command_reports_failure_exit_code() -> None:
    m = measure_ram.measure_command([sys.executable, "-c", "import sys; sys.exit(3)"], interval=0.05)
    assert m.exit_code == 3
    assert "exited with code 3" in measure_ram.render_summary(m)


def test_measure_command_missing_program_raises() -> None:
    with pytest.raises(OSError):
        measure_ram.measure_command(["definitely-not-a-real-program-xyz"])


def test_measure_pid_attaches_to_running_process(tmp_path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", allocating_script(hold_s=2.0)])
    try:
        time.sleep(0.5)
        m = measure_ram.measure_pid(proc.pid, interval=0.1, trace_path=tmp_path / "t.csv", label="attached")
    finally:
        proc.wait()
    assert m.label == "attached"
    assert m.peak_bytes >= 0.9 * ALLOC_MIB * MIB
    assert m.wall_time_s >= 2.0
    assert m.exit_code is None


def test_main_writes_summary_json_and_forwards_exit_code(tmp_path, capsys) -> None:
    summary = tmp_path / "ram" / "summary.json"
    code = measure_ram.main(
        ["-i", "0.1", "--summary", str(summary), "--label", "demo", "--", sys.executable, "-c", allocating_script()]
    )
    assert code == 0
    data = json.loads(summary.read_text())
    assert data["label"] == "demo"
    assert data["peak_bytes"] >= 0.9 * ALLOC_MIB * MIB
    assert "| demo |" in capsys.readouterr().err


def test_main_unknown_pid_returns_error() -> None:
    assert measure_ram.main(["--pid", "999999999"]) == 1


def test_main_missing_program_returns_127() -> None:
    assert measure_ram.main(["--", "definitely-not-a-real-program-xyz"]) == 127


@pytest.mark.parametrize(
    "argv",
    [[], ["--pid", "1", "--", "echo"], ["-i", "0", "--", "echo"]],
)
def test_main_rejects_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        measure_ram.main(argv)
    assert excinfo.value.code == 2


@pytest.mark.parametrize(("returncode", "expected"), [(None, 0), (0, 0), (3, 3), (-9, 137)])
def test_shell_exit_code(returncode: int | None, expected: int) -> None:
    assert measure_ram._shell_exit_code(returncode) == expected


def test_cli_end_to_end_reports_killed_job() -> None:
    script = SCRIPTS_DIR / "measure_ram.py"
    killer = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    if sys.platform == "win32":
        pytest.skip("SIGKILL semantics are POSIX-only")
    result = subprocess.run(
        [sys.executable, str(script), "-i", "0.05", "--", sys.executable, "-c", killer],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 137
    assert "OOM killer" in result.stderr
