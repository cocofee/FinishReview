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
.\.venv\Scripts\python -m pytest -q --basetemp .pytest_tmp_finish_review
.\.venv\Scripts\python -m realtime.review_main
```

`constraints-dev.txt` 固定了已验证的 Windows 开发与打包依赖版本。CI 在 Python 3.10
和 Python 3.12 上执行同一套安装、编译和测试命令，并在 Python 3.12 上构建发布包。

系统需要可用的 FFmpeg。可以把 `ffmpeg.exe` 放在程序目录，加入 `PATH`，或设置：

```powershell
$env:FINISH_REVIEW_FFMPEG = "C:\path\to\ffmpeg.exe"
```

## Package

```powershell
.\packaging\build.ps1
```

输出位于 `artifacts\dist\FinishReviewConsole`。打包脚本显式排除检测、OCR 和模型框架，
并检查发布目录没有混入比赛数据、日志或本机配置。

## Compatibility boundary

`CYCLERACE_DISCOVER_VIDEOPIPE_V1` 是已部署的局域网发现协议标识。它作为兼容字段保留，
不表示本项目仍依赖 VideoPipe 的检测系统。
