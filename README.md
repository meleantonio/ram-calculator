# measuring-replication-ram

An [Agent Skill](https://agentskills.io) that teaches coding agents (Cursor, Claude Code, and others) to
**measure and document how much RAM a research replication package actually needs**, rather than guess.

It is based on Florian Oswald's JPE Data Editor post
["How Much RAM Does Your Replication Package Actually Need?"](https://jpedataeditor.github.io/posts/20261001-ram-usage/)
(October 1, 2026). A replication package that runs fine on the author's workstation can stall, swap for days, or
get `Killed` on a replicator's machine when its README doesn't state a measured peak RAM figure.

[中文说明](README.zh-CN.md)

## What's inside

```
skills/measuring-replication-ram/
├── SKILL.md               # workflow, quick reference, README template, common mistakes, checklist
├── platform-commands.md   # native commands: macOS, Linux, cgroups/Slurm/Docker, Windows; Stata PATH setup
└── scripts/measure_ram.py # cross-platform peak-RAM + RAM-over-time recorder (includes child processes)
tests/                     # pytest suite for measure_ram.py
```

The skill activates when an agent is preparing or auditing a replication package, writing its computational
requirements, or debugging a job that swaps, is OOM-killed, or exceeds a scheduler memory limit.

## Install the skill

Copy the skill folder into your agent's skills directory:

```bash
git clone <this-repo-url> measuring-replication-ram-skill

# Cursor: personal (all projects) or per project
mkdir -p ~/.cursor/skills && cp -r measuring-replication-ram-skill/skills/measuring-replication-ram ~/.cursor/skills/
# or: cp -r measuring-replication-ram-skill/skills/measuring-replication-ram <project>/.cursor/skills/

# Claude Code
mkdir -p ~/.claude/skills && cp -r measuring-replication-ram-skill/skills/measuring-replication-ram ~/.claude/skills/
```

Then ask your agent something like *"Add memory requirements to the README of this replication package"* or
*"My Stata job gets killed on the cluster, how much RAM does it need?"*

## Use the helper script directly

`measure_ram.py` runs a command, sums the resident memory of the command and all of its child processes every
`--interval` seconds, streams a CSV trace, and prints a summary with a README-ready table row. It requires only
Python 3.9+ and `psutil`.

```bash
pip install psutil
python skills/measuring-replication-ram/scripts/measure_ram.py \
    --label "Baseline model" -i 1 \
    -o logs/ram/baseline_trace.csv --summary logs/ram/baseline.json \
    -- stata-mp -b do main.do

# or, with uv (installs psutil automatically from the inline script metadata):
uv run skills/measuring-replication-ram/scripts/measure_ram.py -- Rscript run_all.R

# attach to a job that is already running:
python skills/measuring-replication-ram/scripts/measure_ram.py --pid 12345 -o trace.csv
```

Example output:

```
== measure_ram: Demo step ==
Peak RAM:                    489 MiB  (0.51 GB, 513,175,552 bytes)
  sampled process-tree peak: 489 MiB at 0.2s
  OS single-process peak:    159 MiB
Wall time:                   1.7s
Max concurrent processes:    4  (9 samples)
Measured on:                 Linux 6.12 x86_64, 4 logical CPUs, 16 GiB RAM

README row (GiB = 1024^3 bytes):
| Step | Peak RAM | Wall time | Measured on |
| --- | --- | --- | --- |
| Demo step | 489 MiB | 1.7s | Linux 6.12 x86_64, 4 logical CPUs, 16 GiB RAM |
```

In this run, three workers each held about 150 MiB. `/usr/bin/time` and similar per-process tools would report
only the 159 MiB single-process peak, while the job really needed about 489 MiB. That gap is why the skill
insists on measuring the whole process tree.

The script returns the measured command's exit code (137 for SIGKILL, which is often the OOM killer). Wrappers
and CI can therefore tell a complete measurement from a crashed one.

## Development

```bash
uv sync                       # creates .venv with psutil, pytest, ruff
uv run ruff check . && uv run ruff format --check .
uv run pytest                 # includes coverage; fails below 80%
```

CI (`.github/workflows/ci.yml`) runs lint and tests on Linux, macOS, and Windows.

## Credits and license

The guidance paraphrases and extends the JPE Data Editor post by Florian Oswald. Please cite the original:

```bibtex
@misc{oswald2026,
  author = {Oswald, Florian},
  title  = {How {Much} {RAM} {Does} {Your} {Replication} {Package} {Actually} {Need?}},
  date   = {2026-10-01},
  url    = {https://jpedataeditor.github.io/posts/20261001-ram-usage/}
}
```

Code and skill text in this repository are released under the [MIT License](LICENSE).
