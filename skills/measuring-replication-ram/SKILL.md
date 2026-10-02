---
name: measuring-replication-ram
description: Use when preparing, auditing, or writing the README of a research replication package (Stata, R, MATLAB, Python, Julia), when stating computational or hardware requirements, or when a job swaps for hours, prints "Killed", is OOM-killed, or exceeds a Slurm/PBS/Docker memory limit and nobody has measured how much RAM it really needs.
---

# Measuring RAM for Replication Packages

## Overview

A replicator can only tell whether their machine is big enough if the README states a **measured peak RAM
figure for each step, with units and evidence**. "Lots of RAM" or "my computer has 256 GB" is a guess, and a
replicator only learns it was wrong after a failed run. Swap won't save them: HPC clusters and containers often
disable swap or kill jobs at a hard limit, usually with nothing more than `Killed`.

Based on Florian Oswald's JPE Data Editor post, ["How Much RAM Does Your Replication Package Actually Need?"](https://jpedataeditor.github.io/posts/20261001-ram-usage/) (2026).

## Workflow

1. **List every entry point a replicator runs.** That includes the master script and each model variant
   (baseline, extensions, robustness). A variant with a richer state space needs its own measurement.
2. **Look for measurements you already have.** If the code ran on Slurm or PBS, the accounting database
   (`sacct`, `seff`, `qstat -f`) has probably already recorded each completed job's peak, so no rerun is
   needed. Old `/usr/bin/time` logs count too. See the Slurm caveats in
   [platform-commands.md](platform-commands.md).
3. **Measure what is still missing.** For each entry point, measure two things, always including
   child/worker processes:
   - the **peak RAM** for the whole run (one number), which decides whether the job fits at all;
   - **RAM over time** (a trace), which shows which stage causes the peak. Print a timestamped marker at the
     start of each stage so you can line the trace up with the stages.
4. **Record the result** in the README's computational requirements section (template below).
5. **Commit the evidence** (the run log containing the peak line, plus the trace CSV, plot, or `sacct` output)
   to `logs/` or `outputs/` in the package.
6. **Go through the checklist** at the bottom.

### Measuring

Preferred: run the bundled cross-platform helper. It sums RSS across the whole process tree, streams a CSV
trace (so evidence survives an OOM kill), and prints a ready-to-paste README row:

```bash
set -o pipefail   # so `tee` doesn't hide the exit code (137 = killed)
python scripts/measure_ram.py --label "Baseline model" -o logs/ram/baseline_trace.csv \
    --summary logs/ram/baseline.json -- stata-mp -b do main.do 2>&1 | tee logs/ram/baseline_run.log
# needs psutil (pip install psutil); or: uv run scripts/measure_ram.py -- ...
# already running? python scripts/measure_ram.py --pid <PID> -o trace.csv
```

Alternatives: `psrecord <PID> --interval 5 --log mem.txt --plot mem.png --include-children`, or OS-native
tools. See [platform-commands.md](platform-commands.md) for macOS, Linux, Windows, and cgroup/Slurm commands,
an `sbatch` template, launch lines for MATLAB, Stata, and R, and how to get Stata onto `PATH`.

**If you are an agent:** a full run can take hours and use most of a machine's memory. Ask before you start a
long job. If you can't run it, give the user the exact commands to run. Never write a RAM figure you didn't
measure, even if the user asks for "a reasonable estimate". Use one of these instead:

- **If a full run completed under a memory limit the scheduler enforced** (for example `--mem=256G`), write
  that as an upper bound: "ran to completion with 256 GiB allocated (enforced by Slurm); the actual peak may
  be lower". Cite the job IDs.
- **If only the machine's total RAM is known**, say "ran on a machine with 256 GiB of RAM", plus a visible
  `TODO: measure peak RAM`.
- **Don't extrapolate from a subsample.** Parsing overhead and grid growth don't scale linearly.

## Quick reference: native peak RAM

| Platform | Command | Read this | Unit |
| --- | --- | --- | --- |
| macOS | `/usr/bin/time -l <cmd>` | `maximum resident set size` | **bytes** |
| Linux | `/usr/bin/time -v <cmd>` | `Maximum resident set size (kbytes)` | KiB |
| Linux, running job | `grep VmHWM /proc/<PID>/status` | `VmHWM` | KiB |
| Linux cgroup / Slurm / Docker | `cat /sys/fs/cgroup/<job cgroup>/memory.peak` (v1: `memory.max_usage_in_bytes`); includes page cache | file content | bytes |
| Slurm, finished job | `sacct -j <jobid> --units=G --format=JobID,State,MaxRSS,ReqMem,Elapsed` | `MaxRSS` on the `.batch` row | GiB |
| Windows | `(Get-Process <name>).PeakWorkingSet64`, polled while the process runs | value | bytes |
| any | `ps -o rss= -p <PID>`, polled | value | KiB |

`time`, `VmHWM`, and `PeakWorkingSet64` report the peak of **one process**. With process-based parallelism
(`parpool("Processes")`, `future::plan("multisession")`, `parallel::makeCluster()`, several Stata instances),
use the helper, `psrecord --include-children`, or the cgroup peak instead.

## README template

```markdown
### Memory requirements
Peak RAM measured with `measure_ram.py` (resident set size summed over all processes; 1 GiB = 1024^3 bytes).
Logs and traces: `logs/ram/`.

| Step | Peak RAM | Wall time | Measured on |
| --- | --- | --- | --- |
| Baseline model: solve + simulate | 207 GiB | 6h 40m | 32-core Linux server, 512 GiB RAM |
| Entrepreneur model: solve + simulate | 259 GiB | 9h 15m | 32-core Linux server, 512 GiB RAM |

The entrepreneur model adds a state dimension, which enlarges the state-space grid.
`02_estimate.R` runs 16 worker processes and memory grows roughly with the number of workers;
on a smaller machine, set `workers` lower in `02_estimate.R`.
```

On a cluster, "Measured on" should give the job's allocation (CPUs and `--mem`), not the node's total size.
State the worker count for each parallel step. When you recommend an amount of RAM, add some headroom to the
largest measured peak, and say how much.

## When the number is surprisingly high

The usual cause is holding data that is not needed all at once:

| Pattern | What to try |
| --- | --- |
| Whole dataset loaded at once (`read.csv`, `import delimited`, `readtable`) | Read only the needed columns/rows; process by year, firm, or block |
| Large intermediate arrays (dense grids, value functions, transition matrices) | Watch how each extra state dimension multiplies grid size; use sparse storage; free arrays early |
| Process workers each holding a full copy of the data | N workers ≈ N× memory. Use fewer workers, or thread-based parallelism (`parpool("Threads")`), which shares memory |
| Results accumulated in memory (draws, bootstrap replicates, panel-years) | Write each piece to disk and drop it from memory |
| Silent copy-on-modify (R, MATLAB) in function or transformation chains | Modify in place; avoid keeping intermediate copies around |

These choices are often fine. The point is to measure what they cost instead of assuming.

## Common mistakes

- **Measuring only the parent PID.** Worker processes don't count toward it, so you under-report.
- **Mixing up units.** macOS `time -l` reports bytes, Linux reports KiB, and GB (10^9) is not GiB (1024^3).
  Say which one the README uses.
- **Using the shell builtin `time`.** It has no `-l`/`-v`; call `/usr/bin/time` (on Debian/Ubuntu,
  `apt install time`).
- **Polling the wrong process.** `Rscript` replaces itself with an `R` process on Unix and may start a
  separate `Rterm` process on Windows. Launchers such as `matlab` start the real binary as a separate process.
  Find the process with `pgrep -nf <script name>`, or measure the whole process tree.
- **Reporting a crashed run.** A non-zero exit code or exit 137 (SIGKILL, often the OOM killer) means the
  peak shown is a lower bound, not the requirement. On Slurm, the job state is `OUT_OF_MEMORY` or `FAILED`.
  For a measurement run, request generous `--mem` so the run itself isn't killed.
- **Missing workers that run outside the job.** With a MATLAB cluster profile (MATLAB Parallel Server),
  `parpool` workers are separate scheduler jobs. They don't appear in the process tree or the job's cgroup.
  Measure them through their own jobs.
- **Sampling too sparsely.** Short spikes between samples get missed. Cross-check against an OS high-water
  mark (`time`, `VmHWM`, cgroup `memory.peak`). Note that summed RSS can double-count shared pages, which
  errs on the safe side.

## Checklist

- [ ] Peak RAM measured, not guessed, for every distinct script or model a replicator will run
- [ ] Variants with materially different RAM needs reported separately
- [ ] README gives numbers with units (GB vs GiB stated), plus wall time and the machine used
- [ ] Measurement includes all worker processes when using process-based parallelism
- [ ] Raw evidence (run log with peak line, trace or plot) committed to the package
