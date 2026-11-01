# 边录边判性能基线（现场记录与汇总）

本文只说明 `tools/live_performance_baseline.py` 的用途、30 分钟现场执行方法和验收边界。
工具只读现有日志、`problem_list.json` 和可选进程采样，不启动分析、不改录像/芯片日志/设置，
也不会把汇总结果说成现场验收（报告中的 `field_verified` 始终为 `false`）。

## 能测什么，缺什么

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| 运行时长 | 日志 `FinishReview starting/stopped` | 有起止配对时用配对；日志被轮转截断时退回日志时间跨度 |
| 录像会话 | 日志 `Recording session started/stopped` | 记录起止、机位和累计秒数 |
| 分析启动次数 | 日志 `analysis process starting live=... finalizing=...` | 新日志按事件计数；历史日志没有该事件时为 `unknown`，不用 `live batch` 行代替 |
| 发布/批次轮次 | 日志 `live batch` 行 | 每行只在一次实际发布后打印，算一次发布轮次；不代表所有分析 batch，也不代表 worker 启动次数 |
| 逐轮耗时 | `live batch ... sampling/analysis/publish/total` | 每轮和汇总的中位数、p95、最大值 |
| 新视频时长/处理比 | `live batch ... video`、清单逐机位 `performance` | 见下方“处理比口径”；`rounds` 中带每轮累计视频和相对首轮的时间 |
| 进度/积压 | 每轮 `pending`、清单 `pending_segments`、`sealed_through_ms - analyzed_through_ms` | 验收用每个已知机位的最新 pending 最大值；任一路未归零即未追平 |
| 内部操作延迟/失败 | 退出时 `Runtime metrics` | 仅退出汇总；p50/p95/max、次数、失败数；含解码、目录、界面等内部操作，不只是分析 |
| 分析暂停/ROI 回退 | 日志 `analysis paused`、`ROI decode unavailable` | 只有次数，没有暂停时长 |
| CPU/内存/磁盘 | `sample` 子命令 | CPU 时间增量、工作集、录像盘剩余空间；中位/p95 用最近保留窗口，次数/最小/最大/均值为全程精确值 |

处理比口径：

- 优先用 `live batch` 合计：`sum(total)/sum(video)`；所需机位数优先取 context，缺失时取录像会话日志中的机位列表；
  必须所有机位都出现过，缺一路则为未知，不用剩余机位数字通过。
- 没有可用日志时，退回最后一份 `problem_list.json`，取各机位 `processing_ratio` 的**最大值**（最差机位），
  并要求该清单包含全部所需机位；任一机位缺少处理比则整体为未知。
- 报告中 `processing_ratio_source` 标明来源；`null` 表示无法可靠计算。

明确测不到、报告中列入 `not_measured` 的内容：

- 逐人计数、漏读和误判是否正确，需要回看原帧人工核对；
- 历史日志没有 `analysis process starting` 事件时的分析启动次数（保持未知，不用批次行代替）；
- 操作员点击、判读和整场人工用时；
- 真实芯片接收延迟、断线重连、摄像头故障（日志只能给暂停/错误次数）；
- GUI 实际流畅度（只有退出时 `ui_event_loop_delay` 的汇总）；
- 画面质量、终点线配置是否适合当前赛事；
- 持续磁盘写入吞吐和数小时稳定性；
- 外部 CycleRace 是否同时运行，只能读 `context` 中的人工声明。

## 短自测（不是性能证据）

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -B -m pytest -q tests/tools/test_live_performance_baseline.py
```

测试只校验日志/清单/JSONL 的解析和汇总格式，不启动 30 分钟回放，也不代表现场通过。

## 30 分钟现场执行

前提：与正式使用同一台笔记本、同一录像目录和同一路数（单路或双路），并记录版本和负载。
不要用合成回放、短片段拼接或另一台空闲电脑冒充现场验收。

1. 记录版本：安装包/可执行文件版本或 `git rev-parse --short HEAD`，以及是否打包版。
2. 记录设备：CPU、内存、系统盘/录像盘、是否有独显、屏幕分辨率。
3. 记录路数：单路或双路、分辨率/帧率、录像目录。
4. 记录并发：是否同时运行 CycleRace、是否同时做接收复核、其他占用 CPU/磁盘的程序。
5. 启动 FinishReview 和（如该场景需要）CycleRace，确认日志文件：
   `%LOCALAPPDATA%\FinishReview\logs\finish_review.log`。确认版本会写
   `analysis process starting live=... finalizing=...`；如果日志没有该事件，报告中的
   `analysis_process_starts` 为 `null`（未知），不要把它当作 0，也不要用 `live batch` 行代替。
6. 找到 FinishReview 进程号（任务管理器“详细信息”，或 PowerShell
   `Get-Process FinishReviewConsole | Select-Object Id`），启动只读采样：

```powershell
python -B -m tools.live_performance_baseline sample `
  --pid <PID> --interval 5 --duration 2100 `
  --disk-path "D:\录像目录" `
  --output "$env:TEMP\finishreview-live-samples.jsonl"
```

`--retain`（默认 4096）控制内存中保留的最近样本数，用于中位/p95；次数、最小、最大和均值为
全程精确值，长时间运行不会无限增长内存。

7. 连续运行至少 30 分钟，覆盖真实到达、密集集团和至少一次芯片/录像变化；不要为了“凑时长”暂停分析。
8. 结束录制并正常退出程序，然后汇总（把 `<赛事工作区>` 换成隔离副本或实际工作区）：

```powershell
python -B -m tools.live_performance_baseline summarize `
  --log "$env:LOCALAPPDATA\FinishReview\logs\finish_review.log" `
  --workspace "<赛事工作区>" `
  --sampler "$env:TEMP\finishreview-live-samples.jsonl" `
  --context .\baseline-context.json `
  --output .\baseline-<日期>.json --strict
```

`--log` 也可以传日志目录，工具按修改时间把轮转日志从旧到新读取。保留日志、采样 JSONL、
context 和报告作为原始记录；是否通过由人工签字，不由工具宣称。

现场 context 示例（五个必填项，缺项会进入 `context_missing`）：

```json
{
  "app_version": "0.25.6 / git 3b99347 / 打包 exe",
  "device": "现场笔记本 CPU/内存/系统盘/录像盘/是否有独显",
  "cameras": {"count": 2, "resolution": "1920x1080@25", "sources": "RTSP+USB"},
  "cyclerace_concurrent": true,
  "review_concurrent": true,
  "race_date": "2026-11-01",
  "operator": "姓名或代号",
  "notes": "本场已知异常、断网、重启等"
}
```

## 验收边界

工具按以下目标做“已记录信号”的检查，全部失败项会写进 `acceptance`；未知信号记为 `null`，
不会当成通过，也不会因此让 `--strict` 失败：

- 运行时长达到 `--required-minutes`（默认 30 分钟）；
- `processing_ratio < 1`，并在 `--target-ratio`（默认 0.5）以内；来源必须可靠，不能用单机位或
  不完整的最后一份清单通过；
- 每个已知机位的最新待分析片段都为 0，才算追平积压；任一路仍有积压即为未追平；
- 退出时 `Runtime metrics` 的失败数为 0（没有摘要时为未知；该指标不只统计分析）；
- 现场 context 五项齐全。

这些只是门槛，不等于：

- 30 分钟里每个到达者都被正确计数；
- 操作员实际感觉到界面流畅；
- 摄像机、芯片接收和磁盘在整个比赛中都没有异常；
- 数小时运行或下一场赛事也能通过。

短跑、合成日志、缓存复用和空闲机器上的吞吐量都不能替代“录制 + 芯片接收 + 复核 + 分析
同时进行”的 30 分钟现场记录。报告中的 `not_measured` 和 `missing_signals` 必须随报告一起阅读。

## 结果存放

报告、采样和 context 放在临时目录或赛事隔离副本中，不提交仓库；不要包含密钥、真实运动员
个人信息、录像或芯片原始事件。需要长期比较时，只保留脱敏后的报告和 context。
