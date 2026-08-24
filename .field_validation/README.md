# FinishReview 现场验收清单

本清单用于发布候选和架构拆分前的真实环境验证。普通 CI 不连接赛事硬件。

## 环境记录

- FinishReview 提交 SHA、构建时间、Python/PyInstaller 版本。
- CycleRace 版本与发送端提交 SHA。
- Windows 版本、时区、设备名称和本机 IP。
- USB/RTSP 摄像机型号、分辨率、帧率和固件版本。
- 高速摄像电脑、共享目录路径和两台电脑的时钟偏差。
- 交换机、Wi-Fi、访客设备和 Windows 防火墙规则。

不得在记录中保存 Token、密码、运动员隐私数据或未脱敏的赛事目录。

## 固定脱敏样本

在仓库根目录生成一套确定性样本：

```powershell
.\.venv\Scripts\python.exe -m tools.generate_field_validation_fixture `
  --output .field_validation\generated\sample-2026-08-24
```

输出包括：

- `expected\finish_review`，包含预期赛事元数据和两条 Passage 的只读快照；
- `runtime\finish_review`，用于启动应用并接收回放请求的空运行目录；
- `cyclerace_requests.jsonl`，包含元数据、两条 Passage、一次重复投递和一次焦点消息；
- `auyat\Photo\validation_sample.RGB`，可由现有奥亚特解析器定位到两条样本 Passage；
- `fixture_manifest.json`，记录预期 ACK 顺序及不可变样本文件的 SHA-256；
- `validation_result.json`，所有场景初始状态均为 `not_run`，用于记录真实现场结果。

`expected\finish_review` 只用于检查解析结果，不得作为应用的 `--output`。启动验收实例时，
必须使用空的 `runtime\finish_review`：

```powershell
$fixture = (Resolve-Path .field_validation\generated\sample-2026-08-24).Path
.\.venv\Scripts\python.exe -m realtime.review_main `
  --output "$fixture\runtime\finish_review" `
  --high-speed-dir "$fixture\auyat"
```

保持应用运行，在另一个 PowerShell 窗口回放请求：

```powershell
$fixture = (Resolve-Path .field_validation\generated\sample-2026-08-24).Path
$uri = "http://127.0.0.1:18765/api/v1/passage-events"

Get-Content "$fixture\cyclerace_requests.jsonl" | ForEach-Object {
  $body = ($_ | ConvertFrom-Json) | ConvertTo-Json -Depth 20 -Compress
  $response = Invoke-WebRequest `
    -Uri $uri `
    -Method Post `
    -ContentType "application/json" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
    -UseBasicParsing
  $ack = $response.Content | ConvertFrom-Json
  [PSCustomObject]@{
    HttpStatus = [int]$response.StatusCode
    MessageType = $ack.message_type
    Status = $ack.status
  }
} | Format-Table
```

预期顺序为：元数据 `201 accepted`、第一条 Passage `201 accepted`、重复 Passage
`200 duplicate`、第二条 Passage `201 accepted`、焦点消息 `201 accepted`。回放完成后，
`runtime\finish_review\cyclerace_passage_events.jsonl` 必须正好包含两行。
再次验证时应生成一个新的 fixture 目录；不得向已经产生 JSONL 的运行目录重复执行整组回放，
否则首次 Passage 也会被正确地判定为 `duplicate`，无法验证首次接受路径。

生成器只接受不存在或空目录，不会覆盖已有验收记录。`.field_validation\generated`
默认不进入 Git，现场记录中仍不得填写 Token、密码或真实运动员身份信息。该样本只验证
格式、导入和重复投递语义，不能替代真实 CycleRace、摄像机、共享目录或网络故障验收。
现场填写 `validation_result.json` 时，状态只使用 `not_run`、`passed`、`failed` 或
`blocked`；`blocked` 必须在 `notes` 中记录缺失的设备或环境前置条件。该结果文件属于
现场可变记录，不参与 `fixture_manifest.json` 的 SHA-256 校验。

## 必测流程

| 场景 | 操作 | 完成标准 |
|---|---|---|
| CycleRace 推送 | 发送 Passage、赛事元数据和焦点消息 | 数据落入 JSONL；UI 显示一致；重复消息不重复记账 |
| 双机位录像 | 同时启动主、辅机位并停止 | 两路文件可播放；时间线完整；停止后无残留 FFmpeg 进程 |
| 高速共享目录 | 写入并释放一组脱敏 RGB 样本 | 扫描完成；定位到正确时间；共享目录只读使用 |
| 证据确认 | 对普通视频和高速图像各确认一次 | association JSONL 追加 revision；重启后状态保持 |
| 应用重启 | 录像中结束进程后重新启动 | 开放片段被恢复；已有 Passage 和确认记录不丢失 |
| RTSP 中断 | 录像中断开网络 30 秒后恢复 | UI 明确提示；应用可停止/重启录像；无无限阻塞 |
| CycleRace 慢请求 | 只发送请求头、不发送完整 body | 接收器在配置超时内释放连接并返回 `request_timeout` |
| 共享目录离线 | 扫描中断开高速电脑共享 | UI 可继续操作；恢复共享后可重新扫描 |
| 磁盘不足 | 在隔离测试盘制造低空间条件 | 录像失败有明确提示；既有 JSONL 和视频不被删除 |
| 长时间运行 | 发布候选连续运行至少 2 小时 | 内存、线程、句柄和磁盘增长可解释；无未捕获异常 |

## 性能基线

在固定测试机上执行：

```powershell
.\.venv\Scripts\python.exe -m tools.benchmark_review --sizes 500 2000 5000
```

保存完整 JSON 输出，并记录真实赛事的 Passage 数、视频段数、高速抓拍数、JSONL 大小和首次加载耗时。只有真实 UI 延迟超过约定阈值后，才启动索引优化。

## 验收结论

- 记录每个场景的开始/结束时间、结果、日志路径和脱敏证据路径。
- 任一正式数据丢失、证据错绑、录像无法停止或接收线程无法释放，均判定失败。
- 失败后恢复上一发布包和原配置；不得在现场直接迁移或重写正式 JSONL。
