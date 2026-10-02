# Native RAM measurement commands by platform

Use these when you can't run `scripts/measure_ram.py`. Every method works the same way: either ask the OS for
the run's peak directly, or find the PID and poll it over time.

## Launch lines and process names

| Language | macOS / Linux batch launch | Windows batch launch | Windows process name |
| --- | --- | --- | --- |
| MATLAB | `matlab -batch "run('myscript.m')"` | `Start-Process matlab -ArgumentList '-batch "run(''myscript.m'')"' -Wait` | `MATLAB` |
| Stata | `stata-mp -b do myscript.do` (or `stata-se`, `stata`) | `Start-Process StataMP-64 -ArgumentList '/e do myscript.do' -Wait` (batch flag is `/e`, not `-b`) | `StataMP-64` |
| R | `Rscript myscript.R` | `Start-Process Rscript -ArgumentList 'myscript.R' -Wait` | `Rscript` / `Rterm` |
| Python | `python myscript.py` | `Start-Process python -ArgumentList 'myscript.py' -Wait` | `python` |
| Julia | `julia myscript.jl` | `Start-Process julia -ArgumentList 'myscript.jl' -Wait` | `julia` |

On macOS and Linux, find the PID by matching the script name rather than the binary name:
`PID=$(pgrep -nf myscript)`. This avoids two traps: `Rscript` runs as a process named `R`, and the `matlab`
launcher starts a separate `MATLAB` binary. On Windows, check Task Manager's Details tab to see which process
actually grows.

## macOS

```bash
# Peak RAM for the whole run (largest single process). Unit: BYTES.
/usr/bin/time -l Rscript myscript.R 2>&1 | tee run.log
# -> "maximum resident set size" near the bottom of run.log

# RAM over time (ps reports RSS in KiB)
PID=$(pgrep -nf myscript.R)
while kill -0 "$PID" 2>/dev/null; do
  echo "$(date +%s) $(ps -o rss= -p "$PID")"
  sleep 5
done > mem_trace.log

# Activity Monitor's "Memory" figure, no sudo needed (ps rss reads slightly higher):
footprint "$PID"

# With process workers, list the tree and sum their RSS:
pstree -p "$PID"     # brew install pstree
```

## Linux

```bash
# Peak RAM for the whole run (largest single process). Unit: KiB.
# Requires GNU time: the shell builtin `time` has no -v (Debian/Ubuntu: apt install time).
/usr/bin/time -v stata-mp -b do myscript.do 2>&1 | tee run.log
# -> "Maximum resident set size (kbytes)" near the bottom of run.log

# RAM over time
PID=$(pgrep -nf myscript.do)
while kill -0 "$PID" 2>/dev/null; do
  echo "$(date +%s) $(ps -o rss= -p "$PID")"
  sleep 5
done > mem_trace.log

# Forgot to wrap the launch? The kernel tracks a running peak (high-water mark):
grep VmHWM /proc/"$PID"/status

# Sum RSS (KiB) over the process and its direct children (workers):
ps -o rss= -p "$PID" --ppid "$PID" | awk '{s+=$1} END {print s}'
pstree -p "$PID"   # inspect the full tree if workers spawn their own children
```

### Containers and schedulers (cgroups)

Under Slurm, PBS, or Docker, the cgroup records a true peak covering every process in the job, so there is
no polling and no race with process exit. This is the number the scheduler compares against its memory limit.
Two caveats:

- It **includes page cache**, for example from reading a 40 GB CSV, so it can be well above summed RSS. Say
  which measure you report.
- Inside `sbatch`, `/proc/self/cgroup` points to the **step's** cgroup. The whole job's cgroup is a parent
  directory.

```bash
cat /proc/self/cgroup                                # find your cgroup path
cat /sys/fs/cgroup/<path>/memory.peak                # cgroup v2 (kernel >= 5.19), bytes
cat /sys/fs/cgroup/memory/<path>/memory.max_usage_in_bytes   # cgroup v1, bytes
docker stats --no-stream <container>                 # Docker, live
```

### Slurm: use the accounting records you already have

```bash
# All your recent jobs (adjust the start date), then one job in detail
sacct -u "$USER" -S 2026-01-01 --units=G \
  --format=JobID,JobName%30,State,ExitCode,ReqMem,MaxRSS,Elapsed,AllocCPUS,NodeList | tee logs/ram/sacct_all.txt
sacct -j <jobid> --units=G --format=JobID,JobName%30,State,ExitCode,ReqMem,MaxRSS,Elapsed,AllocCPUS
seff <jobid>                                   # summary, if your cluster has it
scontrol show node <node> | grep -E 'CPUTot|RealMemory'   # node hardware, for the README
```

How to read `sacct` output:

- **Where to look:** with `sbatch`, `MaxRSS` is on the `<jobid>.batch` row; the top-level row is often blank.
  Processes started inside the batch script, such as `future`/`parallel` workers, are included.
- **Multi-task steps** (`srun -n`): `MaxRSS` is the maximum over tasks, not the sum. Use `TRESUsageInTot` if
  your Slurm version has it.
- **Sampling:** memory is sampled every `JobAcctGatherFrequency` seconds (default 30), so short spikes can be
  missed. Check the setting with `scontrol show config | grep JobAcctGather`.
- **Only `COMPLETED` jobs with exit code `0:0` count.** `OUT_OF_MEMORY`, `FAILED`, or `TIMEOUT` give only a
  lower bound.
- **Units:** with `--units=G`, "G" means GiB. On older Slurm versions, a `c`/`n` suffix on `ReqMem` means per
  CPU or per node.
- **Retention:** accounting records are purged after a site-specific period. If `sacct` returns nothing,
  measure again.

### Slurm: a measurement job

```bash
#!/bin/bash
#SBATCH --job-name=ram_baseline
#SBATCH --cpus-per-task=17          # e.g. 16 workers + the main process
#SBATCH --mem=256G                  # generous, so the measurement run itself isn't killed
#SBATCH --time=24:00:00
#SBATCH --output=logs/ram/%x_%j.out
set -o pipefail
module load R matlab                # site-specific
# psutil: pip install --user psutil (or use a module/conda env if the cluster has no internet access)
python scripts/measure_ram.py --label "Baseline model" -i 5 \
  -o logs/ram/baseline_trace.csv --summary logs/ram/baseline.json \
  -- matlab -batch "run('model/solve_baseline.m')"
```

After the job ends, run `sacct -j <jobid>` as a cross-check and commit both outputs. `measure_ram.py`
records the job's CPU and memory allocation, read from the Slurm environment variables and the cgroup limit,
next to the node's totals.

## Windows (PowerShell)

Windows keeps a per-process peak working set (`PeakWorkingSet64`, in bytes). The value disappears when the
process exits, so poll it while the process runs and keep the last reading:

```powershell
$name = "StataMP-64"   # or MATLAB, Rscript/Rterm, python, julia
Start-Process StataMP-64 -ArgumentList '/e do myscript.do'
Start-Sleep -Seconds 5
$peak = 0
while ($procs = Get-Process $name -ErrorAction SilentlyContinue) {
    $ws   = ($procs | Measure-Object WorkingSet64 -Sum).Sum
    $peak = [Math]::Max($peak, ($procs | Measure-Object PeakWorkingSet64 -Sum).Sum)
    "$(Get-Date -UFormat %s) $ws" | Out-File -Append mem_trace.log
    Start-Sleep -Seconds 5
}
"Peak (sum of per-process peaks, bytes): $peak"
```

The sum covers parallel workers that share a name, for example several `Rscript` processes from
`parallel::makeCluster()` or `future::plan("multisession")`. Summing per-process peaks overstates the true
peak when the workers don't peak at the same moment, so the figure errs on the safe side.

No-script alternative: in Task Manager's **Details** tab (or Resource Monitor), right-click a column header,
add **Peak working set**, and watch it live.

## Getting Stata onto `PATH`

- **macOS:** Stata is an app bundle, so link the binary into a directory that is already on `PATH`, then open a
  new terminal:

  ```bash
  sudo ln -s "/Applications/Stata/StataMP.app/Contents/MacOS/StataMP" /usr/local/bin/stata-mp
  # StataSE.app/StataSE or StataIC.app/StataIC for other editions
  ```

- **Linux:** the installer (`stinit`) usually offers to create the `stata-mp`/`stata-se` links. If it didn't:

  ```bash
  echo 'export PATH="/usr/local/stata18:$PATH"' >> ~/.bashrc && source ~/.bashrc   # adjust version/path
  ```

- **Windows:** add the install directory (for example `C:\Program Files\Stata18`) under System Properties →
  Environment Variables → Path, then open a new terminal. Or call the full path to `StataMP-64.exe` directly.
