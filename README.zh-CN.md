# measuring-replication-ram（复现包内存测量技能）

这是一个 [Agent Skill](https://agentskills.io)，用于指导编码智能体（Cursor、Claude Code 等）**实际测量并记录科研复现包所需的内存（RAM）**，而不是凭感觉估计。

内容基于 Florian Oswald 在 JPE Data Editor 博客上的文章
[《How Much RAM Does Your Replication Package Actually Need?》](https://jpedataeditor.github.io/posts/20261001-ram-usage/)（2026 年 10 月 1 日）。
如果 README 没有给出实测的内存峰值，在作者电脑上运行正常的代码可能在复现者的机器上卡死、长时间使用交换空间，或被系统直接 `Killed`。

[English](README.md)

## 内容

```
skills/measuring-replication-ram/
├── SKILL.md               # 工作流程、速查表、README 模板、常见错误、检查清单
├── platform-commands.md   # macOS / Linux / cgroup·Slurm·Docker / Windows 原生命令；Stata PATH 配置
└── scripts/measure_ram.py # 跨平台内存峰值与内存随时间变化记录工具（包含子进程）
tests/                     # measure_ram.py 的 pytest 测试
```

## 安装

将技能目录复制到智能体的 skills 目录：

```bash
origin repo clone meleantonio/ram-calculator
cd ram-calculator
# Cursor
mkdir -p ~/.cursor/skills && cp -r skills/measuring-replication-ram ~/.cursor/skills/
# Claude Code
mkdir -p ~/.claude/skills && cp -r skills/measuring-replication-ram ~/.claude/skills/
```

## 直接使用脚本

```bash
pip install psutil
python skills/measuring-replication-ram/scripts/measure_ram.py --label "基准模型" \
    -o logs/ram/baseline_trace.csv --summary logs/ram/baseline.json -- stata-mp -b do main.do
# 附加到正在运行的进程：
python skills/measuring-replication-ram/scripts/measure_ram.py --pid 12345 -o trace.csv
```

脚本每隔 `--interval` 秒统计命令及其所有子进程的常驻内存总和，实时写入 CSV 轨迹文件，并输出摘要以及可直接粘贴到 README 的表格行。脚本的退出码与被测命令一致（137 表示被 SIGKILL 终止，通常是 OOM）。

## 开发

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

## 致谢与许可

指导内容改写并扩展自 Florian Oswald 的 JPE Data Editor 博客文章，引用时请注明原文。本仓库代码与技能文本采用 [MIT 许可证](LICENSE)。
