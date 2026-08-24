# FinishReview

独立的终点多源复核系统。该项目只保留人工复核、录像缓冲、视频回放、
CycleRace / RaceTiger 过线事件接入、奥亚特高速相机文件读取和证据关联能力。

本项目不包含目标检测、YOLO、Ultralytics、Torch、OCR、模型权重、训练数据或
自动生成正式比赛成绩的逻辑。CycleRace 仍是正式计时和成绩的唯一权威。

## Source baseline

- Source repository: `cocofee/VideoPipe`
- Source branch: `cocofee/issue-80`
- Source commit: `eaf96848a4a0f11d00d0085908e730a7f24e5da0`
- Extracted on: `2026-08-22`

为降低首次拆分风险，代码暂时保留原来的 `realtime` 包名。后续功能开发应在本项目
进行，不再依赖完整 VideoPipe 仓库。

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]" -c constraints-dev.txt
.\.venv\Scripts\python -m ruff check realtime tests tools
.\.venv\Scripts\python -m pytest -q --basetemp .pytest_tmp_finish_review
.\.venv\Scripts\python -m coverage run -m pytest -q --basetemp .pytest_tmp_coverage
.\.venv\Scripts\python -m coverage report
.\.venv\Scripts\python -m realtime.review_main
```

`constraints-dev.txt` 固定了已验证的 Windows 开发与打包依赖版本。CI 在 Python 3.10
和 Python 3.12 上执行同一套安装、编译和测试命令，并在 Python 3.12 上构建发布包及
执行打包后 EXE 冒烟检查。Ruff 只阻断未定义名称、局部变量先引用以及明确的语法和
控制流错误；coverage 只报告当前覆盖率，不设置发布门槛。

系统需要可用的 FFmpeg。可以把 `ffmpeg.exe` 放在程序目录，加入 `PATH`，或设置：

```powershell
$env:FINISH_REVIEW_FFMPEG = "C:\path\to\ffmpeg.exe"
```

## Package

```powershell
.\packaging\build.ps1
```

脚本优先使用仓库 `.venv\Scripts\python.exe`。需要使用其他隔离环境时显式指定：

```powershell
.\packaging\build.ps1 -PythonPath C:\path\to\python.exe
```

输出位于 `artifacts\dist\FinishReviewConsole`。打包脚本显式排除检测、OCR 和模型框架，
并检查发布目录没有混入比赛数据、日志、本机配置或已知的非项目依赖。

## Performance and field validation

合成性能基线不进入默认 pytest，可在固定测试机上手动执行：

```powershell
.\.venv\Scripts\python.exe -m tools.benchmark_review --sizes 500 2000 5000
```

真实设备、网络故障、磁盘不足和长时间运行的发布前检查见
`.field_validation\README.md`。合成基准不能替代真实赛事目录和现场硬件验收。

## Runtime security and diagnostics

- 运行日志保存在 `%LOCALAPPDATA%\FinishReview\logs\finish_review.log`，自动轮转并保留
  最多 5 个历史文件。
- RaceTiger 令牌使用当前 Windows 用户的 DPAPI 加密后写入配置；旧版明文令牌会在
  下次保存设置时迁移。
- DPAPI 密文不能跨 Windows 用户或电脑直接复用；复制配置后需要重新输入令牌。
- RaceTiger 远程地址必须使用 HTTPS，仅 `localhost` 和回环 IP 允许 HTTP。
- 当前 CycleRace 兼容链路尚未启用身份认证，只能部署在受信任的赛事局域网中。

## Compatibility boundary

`CYCLERACE_DISCOVER_VIDEOPIPE_V1` 是已部署的局域网发现协议标识。它作为兼容字段保留，
不表示本项目仍依赖 VideoPipe 的检测系统。
