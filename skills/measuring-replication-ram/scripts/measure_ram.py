#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["psutil>=5.9"]
# ///
"""Measure the peak RAM and RAM-over-time of a command or running process, including all child processes.

Works on macOS, Linux and Windows. Samples the summed resident set size (RSS) of the whole process tree on a
timer, streams every sample to a CSV trace (so the evidence survives even if the job is OOM-killed), and prints a
summary plus a ready-to-paste README table row.

Examples:
    python measure_ram.py --label "Baseline model" -- stata-mp -b do main.do
    python measure_ram.py -i 1 -o logs/ram/trace.csv -- Rscript run_all.R
    python measure_ram.py --pid 12345 -o trace.csv          # attach to a job that is already running
    uv run measure_ram.py -- matlab -batch "run('main.m')"   # uv installs psutil on the fly
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import psutil
except ImportError:  # pragma: no cover - depends on the user's environment
    sys.exit("measure_ram.py needs psutil: `pip install psutil`, or run it with `uv run measure_ram.py ...`")

try:
    import resource
except ImportError:  # Windows has no `resource` module
    resource = None  # type: ignore[assignment]

LOGGER = logging.getLogger("measure_ram")

GIB = 1024**3
GB = 10**9
TRACE_HEADER = ("unix_time", "elapsed_s", "rss_bytes", "rss_gib", "n_processes")


@dataclass
class TreeSample:
    """One observation of a process tree.

    Attributes:
        rss_bytes: Summed RSS of the root process and all of its live descendants.
        n_processes: Number of processes that could be read.
        os_peak_bytes: Largest OS-tracked per-process high-water mark seen in the tree (Linux VmHWM, Windows peak
            working set); 0 where the OS does not expose it (macOS).
    """

    rss_bytes: int
    n_processes: int
    os_peak_bytes: int


@dataclass
class Measurement:
    """Result of one monitored run.

    Attributes:
        label: Human-readable name of the step, used in the README row.
        command: The command line that was run, or a description of the attached PID.
        peak_bytes: Headline peak: the larger of the sampled tree peak and the OS-reported single-process peak.
        sampled_tree_peak_bytes: Maximum summed RSS of the process tree over all samples.
        os_single_process_peak_bytes: OS-reported high-water mark of the largest single process.
        peak_at_s: Seconds since start at which the sampled tree peak occurred.
        wall_time_s: Wall-clock duration of the run.
        n_samples: Number of samples taken.
        max_processes: Largest number of concurrent processes seen in the tree.
        exit_code: Exit code of the command (None in attach mode or when unknown).
        machine: Description of the machine the measurement was taken on.
    """

    label: str
    command: str
    peak_bytes: int = 0
    sampled_tree_peak_bytes: int = 0
    os_single_process_peak_bytes: int = 0
    peak_at_s: float = 0.0
    wall_time_s: float = 0.0
    n_samples: int = 0
    max_processes: int = 0
    exit_code: int | None = None
    machine: dict[str, Any] = field(default_factory=dict)

    def record(self, sample: TreeSample, elapsed_s: float) -> None:
        """Fold one sample into the running maxima."""
        self.n_samples += 1
        self.max_processes = max(self.max_processes, sample.n_processes)
        self.os_single_process_peak_bytes = max(self.os_single_process_peak_bytes, sample.os_peak_bytes)
        if sample.rss_bytes > self.sampled_tree_peak_bytes:
            self.sampled_tree_peak_bytes = sample.rss_bytes
            self.peak_at_s = elapsed_s
        self.peak_bytes = max(self.sampled_tree_peak_bytes, self.os_single_process_peak_bytes)


def format_bytes(n_bytes: int) -> str:
    """Format a byte count in binary units (GiB = 1024**3 bytes), e.g. ``'12.3 GiB'``."""
    if n_bytes >= GIB:
        return f"{n_bytes / GIB:.1f} GiB"
    if n_bytes >= 1024**2:
        return f"{n_bytes / 1024**2:.0f} MiB"
    return f"{n_bytes / 1024:.0f} KiB"


def format_duration(seconds: float) -> str:
    """Format seconds as a compact duration, e.g. ``'6h 40m'``, ``'3m 12s'`` or ``'4.2s'``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def _leading_int(value: str | None) -> int | None:
    """Parse the leading integer of values like ``'16'`` or ``'16(x2)'`` (Slurm's per-node CPU lists)."""
    digits = ""
    for char in (value or "").strip():
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def cgroup_memory_limit(
    proc_cgroup: Path = Path("/proc/self/cgroup"), cgroup_root: Path = Path("/sys/fs/cgroup")
) -> int | None:
    """Tightest memory limit (bytes) imposed on this process by its cgroup and parents, or None if unlimited."""
    try:
        lines = proc_cgroup.read_text(encoding="ascii").splitlines()
    except OSError:
        return None

    candidates: list[Path] = []
    for line in lines:
        hierarchy, _, rest = line.partition(":")
        controllers, _, rel_path = rest.partition(":")
        rel = rel_path.lstrip("/")
        if hierarchy == "0" and controllers == "":
            node = cgroup_root / rel
            while True:
                candidates.append(node / "memory.max")
                if node == cgroup_root:
                    break
                node = node.parent
        elif "memory" in controllers.split(","):
            candidates.append(cgroup_root / "memory" / rel / "memory.limit_in_bytes")

    limits = []
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if text.isdigit():
            limits.append(int(text))
    return min(limits) if limits else None


def detect_allocation(total_ram_bytes: int, logical_cpus: int | None) -> dict[str, int]:
    """CPUs and memory actually granted to this job by Slurm or a cgroup, when smaller than the whole machine.

    On a cluster the node's totals overstate what the job had, so the README should report the allocation.
    """
    env = os.environ
    allocation: dict[str, int] = {}

    cpus = _leading_int(env.get("SLURM_CPUS_PER_TASK")) or _leading_int(env.get("SLURM_JOB_CPUS_PER_NODE"))
    if cpus is None and hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    if cpus is not None and (logical_cpus is None or cpus < logical_cpus):
        allocation["cpus"] = cpus

    mem_mib = _leading_int(env.get("SLURM_MEM_PER_NODE"))
    per_cpu_mib = _leading_int(env.get("SLURM_MEM_PER_CPU"))
    if mem_mib is None and per_cpu_mib is not None:
        mem_mib = per_cpu_mib * allocation.get("cpus", 1)
    limit = mem_mib * 1024**2 if mem_mib is not None else cgroup_memory_limit()
    if limit is not None and limit < total_ram_bytes:
        allocation["memory_limit_bytes"] = limit
    return allocation


def describe_machine() -> dict[str, Any]:
    """Collect the facts a replicator needs to compare their machine with the one used for measuring."""
    total_ram = psutil.virtual_memory().total
    logical_cpus = psutil.cpu_count(logical=True)
    machine: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "logical_cpus": logical_cpus,
        "total_ram_bytes": total_ram,
        "python": platform.python_version(),
    }
    if "SLURM_JOB_ID" in os.environ:
        machine["slurm_job_id"] = os.environ["SLURM_JOB_ID"]
    allocation = detect_allocation(total_ram, logical_cpus)
    if allocation:
        machine["allocation"] = allocation
    return machine


def machine_summary(machine: dict[str, Any]) -> str:
    """One-line machine description for the README table, including the job allocation when there is one."""
    ram = machine.get("total_ram_bytes", 0) / GIB
    os_arch = f"{machine.get('os', '?')} {machine.get('arch', '')}".strip()
    summary = f"{os_arch}, {machine.get('logical_cpus', '?')} logical CPUs, {ram:.0f} GiB RAM"
    allocation = machine.get("allocation") or {}
    granted = []
    if "cpus" in allocation:
        granted.append(f"{allocation['cpus']} CPUs")
    if "memory_limit_bytes" in allocation:
        granted.append(format_bytes(allocation["memory_limit_bytes"]))
    if granted:
        summary += f" (job allocation: {', '.join(granted)})"
    return summary


def readme_row(measurement: Measurement) -> str:
    """Markdown row matching the table header ``| Step | Peak RAM | Wall time | Measured on |``."""
    return (
        f"| {measurement.label} | {format_bytes(measurement.peak_bytes)} | "
        f"{format_duration(measurement.wall_time_s)} | {machine_summary(measurement.machine)} |"
    )


def _linux_high_water_mark(pid: int) -> int:
    """Return VmHWM (kernel-tracked peak RSS) for ``pid`` in bytes, or 0 if unavailable."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _process_high_water_mark(proc: psutil.Process, mem: Any) -> int:
    """OS-tracked peak memory of a single process, in bytes (0 where the OS doesn't track it)."""
    if sys.platform == "win32":
        return int(getattr(mem, "peak_wset", 0))
    if sys.platform.startswith("linux"):
        return _linux_high_water_mark(proc.pid)
    return 0


def sample_tree(root: psutil.Process) -> TreeSample:
    """Sum RSS over ``root`` and every descendant that is still alive."""
    try:
        procs = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return TreeSample(0, 0, 0)
    except psutil.AccessDenied:
        procs = [root]

    total = count = os_peak = 0
    for proc in procs:
        try:
            mem = proc.memory_info()
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
        total += mem.rss
        count += 1
        os_peak = max(os_peak, _process_high_water_mark(proc, mem))
    return TreeSample(total, count, os_peak)


def _rusage_children_peak() -> int:
    """Peak RSS of the largest reaped descendant as reported by the OS, in bytes (POSIX only)."""
    if resource is None:
        return 0
    max_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(max_rss if sys.platform == "darwin" else max_rss * 1024)


class TraceWriter:
    """Streams samples to a CSV file, flushing every row so the trace survives a crash or OOM kill."""

    def __init__(self, path: Path | None) -> None:
        self._file = None
        self._writer = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(TRACE_HEADER)

    def write(self, sample: TreeSample, elapsed_s: float) -> None:
        if self._writer is None or self._file is None:
            return
        self._writer.writerow(
            (
                f"{time.time():.3f}",
                f"{elapsed_s:.3f}",
                sample.rss_bytes,
                f"{sample.rss_bytes / GIB:.4f}",
                sample.n_processes,
            )
        )
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _monitor(
    root: psutil.Process,
    measurement: Measurement,
    trace: TraceWriter,
    interval: float,
    start: float,
    wait_for_exit: Callable[[float], bool],
) -> None:
    """Sample ``root`` every ``interval`` seconds until ``wait_for_exit`` reports that it has finished."""
    while True:
        sample = sample_tree(root)
        elapsed = time.monotonic() - start
        if sample.n_processes:
            measurement.record(sample, elapsed)
            trace.write(sample, elapsed)
        if wait_for_exit(interval):
            return


def measure_command(
    command: list[str], interval: float = 1.0, trace_path: Path | None = None, label: str | None = None
) -> Measurement:
    """Run ``command`` to completion while sampling the RAM of its whole process tree.

    Args:
        command: Program and arguments, e.g. ``["Rscript", "main.R"]``.
        interval: Seconds between samples. Short spikes between samples can be missed; the OS-reported
            single-process peak is folded in to catch those where the platform provides it.
        trace_path: Optional CSV file that receives one row per sample.
        label: Name for the step in the summary; defaults to the command line.

    Note:
        The POSIX ``RUSAGE_CHILDREN`` peak covers every child this Python process has ever reaped, so call this
        once per interpreter (as the CLI does) if you rely on that number.

    Returns:
        The completed measurement, including the command's exit code.
    """
    command_line = subprocess.list2cmdline(command) if sys.platform == "win32" else " ".join(command)
    measurement = Measurement(label=label or command_line, command=command_line, machine=describe_machine())
    trace = TraceWriter(trace_path)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(command)
    except OSError:
        trace.close()
        LOGGER.exception("Could not start %r - is it installed and on PATH?", command[0])
        raise

    def wait_for_exit(timeout: float) -> bool:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    try:
        root = psutil.Process(proc.pid)
        _monitor(root, measurement, trace, interval, start, wait_for_exit)
    except psutil.NoSuchProcess:
        proc.wait()
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted - waiting for the command to stop and writing partial results")
        proc.wait()
    finally:
        trace.close()

    measurement.wall_time_s = time.monotonic() - start
    measurement.exit_code = proc.returncode
    measurement.os_single_process_peak_bytes = max(measurement.os_single_process_peak_bytes, _rusage_children_peak())
    measurement.peak_bytes = max(measurement.sampled_tree_peak_bytes, measurement.os_single_process_peak_bytes)
    return measurement


def measure_pid(
    pid: int, interval: float = 1.0, trace_path: Path | None = None, label: str | None = None
) -> Measurement:
    """Attach to an already-running process and sample its process tree until it exits.

    Wall time is measured from the process's own start time, so it is correct even if you attach late. Memory
    used before attaching is only captured where the OS tracks a high-water mark (Linux, Windows).
    """
    root = psutil.Process(pid)
    measurement = Measurement(
        label=label or f"PID {pid}", command=" ".join(root.cmdline()) or f"PID {pid}", machine=describe_machine()
    )
    process_started = root.create_time()
    trace = TraceWriter(trace_path)
    start = time.monotonic() - (time.time() - process_started)

    def wait_for_exit(timeout: float) -> bool:
        try:
            root.wait(timeout=timeout)
            return True
        except psutil.TimeoutExpired:
            return False

    try:
        _monitor(root, measurement, trace, interval, start, wait_for_exit)
    except KeyboardInterrupt:
        LOGGER.warning("Stopped monitoring PID %d; the process keeps running. Writing partial results.", pid)
    finally:
        trace.close()

    measurement.wall_time_s = time.time() - process_started
    return measurement


def render_summary(measurement: Measurement) -> str:
    """Human-readable summary block, ending with a README table row."""
    lines = [
        f"== measure_ram: {measurement.label} ==",
        f"Peak RAM:                    {format_bytes(measurement.peak_bytes)}"
        f"  ({measurement.peak_bytes / GB:.2f} GB, {measurement.peak_bytes:,} bytes)",
        f"  sampled process-tree peak: {format_bytes(measurement.sampled_tree_peak_bytes)}"
        f" at {format_duration(measurement.peak_at_s)}",
        f"  OS single-process peak:    {format_bytes(measurement.os_single_process_peak_bytes)}",
        f"Wall time:                   {format_duration(measurement.wall_time_s)}",
        f"Max concurrent processes:    {measurement.max_processes}  ({measurement.n_samples} samples)",
        f"Measured on:                 {machine_summary(measurement.machine)}",
    ]
    if measurement.exit_code is not None and measurement.exit_code != 0:
        lines.append(f"WARNING: command exited with code {measurement.exit_code}; this is not a complete run.")
        if measurement.exit_code in (-9, 137):
            lines.append("         Signal 9 (SIGKILL) usually means the OOM killer or a scheduler memory limit.")
    lines += [
        "",
        "README row (GiB = 1024^3 bytes):",
        "| Step | Peak RAM | Wall time | Measured on |",
        "| --- | --- | --- | --- |",
    ]
    lines.append(readme_row(measurement))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure peak RAM (including child processes) of a command or a running PID.",
        epilog="Put `--` before the command so its own flags are not parsed: measure_ram.py -- Rscript main.R",
    )
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="seconds between samples (default: 1)")
    parser.add_argument("-o", "--trace", type=Path, help="CSV file for the RAM-over-time trace")
    parser.add_argument("--summary", type=Path, help="JSON file for the summary")
    parser.add_argument("--label", help="name of the step for the README row (default: the command)")
    parser.add_argument("--pid", type=int, help="attach to an already-running process instead of launching one")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after `--`")
    return parser


def _shell_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 0
    return 128 - returncode if returncode < 0 else returncode


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the measured command's exit code so wrappers and CI can react to failures."""
    logging.basicConfig(level=logging.INFO, format="measure_ram: %(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.pid is not None and command:
        parser.error("use either --pid or a command, not both")
    if args.pid is None and not command:
        parser.error("give a command to run (after `--`) or --pid")

    try:
        if args.pid is not None:
            measurement = measure_pid(args.pid, args.interval, args.trace, args.label)
        else:
            measurement = measure_command(command, args.interval, args.trace, args.label)
    except psutil.NoSuchProcess:
        LOGGER.error("No process with PID %s", args.pid)
        return 1
    except OSError:
        return 127

    print(render_summary(measurement), file=sys.stderr)
    if args.trace:
        LOGGER.info("RAM-over-time trace written to %s", args.trace)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(asdict(measurement), indent=2) + "\n", encoding="utf-8")
        LOGGER.info("Summary written to %s", args.summary)
    return _shell_exit_code(measurement.exit_code)


if __name__ == "__main__":
    sys.exit(main())
