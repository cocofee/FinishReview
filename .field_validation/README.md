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

## 赛事局域网配置与排障

### 当前地址规划

正式比赛设备使用独立的 `192.168.50.0/24` 有线网络。当前现场地址如下：

| 设备 | 地址 | 主要服务 |
|---|---|---|
| FinishReview 电脑 | `192.168.50.10/24` | CycleRace 接收端口 `TCP 18765` |
| CycleRace 电脑 | `192.168.50.20/24` | 正式计时和成绩权威 |
| 高速摄像电脑 | `192.168.50.30/24` | Windows 共享目录 `TCP 445` |
| 普通摄像机 1 | `192.168.50.63/24` | RTSP `TCP 554` |
| 普通摄像机 2 | `192.168.50.64/24` | RTSP `TCP 554` |
| 普通摄像机 3 | `192.168.50.65/24` | RTSP `TCP 554` |

同一网段内地址必须唯一。设备发现工具能发现摄像机，只能证明二层广播可达，不能证明
网页、RTSP、CycleRace 接收服务或共享目录可用。修改重复 IP 时一次只连接或修改一台设备，
完成后重新扫描并记录设备序列号、IP 和网卡 MAC 的对应关系。

专用赛事网卡统一使用子网掩码 `255.255.255.0`，通常不填写默认网关和 DNS。互联网访问
由另一张 Wi-Fi 或有线网卡承担，不要把 `192.168.0.x` 高速相机直连网段、
`192.168.1.x` 办公网段和 `192.168.50.x` 赛事网段混为同一个网络。

### 千兆链路检查

更换千兆交换机后，先在每台 Windows 电脑的 PowerShell 中检查实际承载赛事网络的物理网卡：

```powershell
Get-NetAdapter -Physical |
  Format-Table Name, InterfaceDescription, Status, LinkSpeed, MacAddress
```

完成标准：赛事网卡显示 `Up` 和 `1 Gbps` 或更高。`Disconnected`、`0 bps` 表示物理链路
未建立，应先检查网线、交换机端口和网口指示灯。电脑同时存在旧百兆网卡、USB 千兆网卡
和 Wi-Fi 时，以实际配置 `192.168.50.x` 的网卡为准，不能仅根据名称“以太网”判断。

确认赛事地址绑定到正确网卡：

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -like "192.168.50.*" } |
  Format-Table InterfaceAlias, IPAddress, PrefixLength
```

`1 Gbps` 只表示电脑与交换机的协商速率，不等于端到端实际吞吐量。现场至少还要完成丢包、
业务端口和真实录像联调；需要测满带宽时，在两台有线电脑间使用 `iperf3`，不要用 Ping 延迟
推断千兆吞吐量。

### 分层排障顺序

必须按以下顺序排查，避免把防火墙、服务未启动或账号错误误判为交换机故障。

1. **物理链路：** `Get-NetAdapter -Physical` 必须显示赛事网卡 `Up`。
2. **地址配置：** `ipconfig` 必须显示预期的 `192.168.50.x/24`，且没有重复 IP。
3. **基础连通：** 两台电脑和摄像机之间互相 Ping，记录延迟和丢包。
4. **业务端口：** 使用 `Test-NetConnection` 检查实际服务端口。
5. **应用状态：** 服务端确认端口正在监听，客户端再执行真实连接。
6. **认证和数据：** 最后检查账号密码、共享权限、RTSP 路径和真实画面。

常用端口检查：

```powershell
# 在 CycleRace 电脑检查 FinishReview 接收服务
Test-NetConnection 192.168.50.10 -Port 18765

# 在 FinishReview 电脑检查高速摄像电脑共享服务
Test-NetConnection 192.168.50.30 -Port 445

# 在录像电脑检查三台普通摄像机 RTSP
Test-NetConnection 192.168.50.63 -Port 554
Test-NetConnection 192.168.50.64 -Port 554
Test-NetConnection 192.168.50.65 -Port 554
```

完成标准为 `TcpTestSucceeded : True`。Ping 成功只证明 IP 层可达；Ping 成功但端口失败时，
检查服务是否启动、是否监听正确地址以及 Windows 防火墙入站规则。

在 FinishReview 电脑检查接收服务：

```powershell
Get-NetTCPConnection -LocalPort 18765 -State Listen
```

正常情况下应监听 `0.0.0.0:18765`。如果没有输出，先启动 FinishReview 或检查应用状态，
不要先修改交换机或放宽整机防火墙。

### 单向 Ping 与防火墙

一台电脑能 Ping 对方、对方却不能反向 Ping，通常表示被 Ping 电脑的 Windows 防火墙阻止
ICMP 入站。Ping 不是 FinishReview 的业务前置条件；即使 Ping 被禁，也应单独检查 `18765`、
`445` 和 `554`。

仅在受信任的赛事局域网中，可在目标电脑的管理员 PowerShell 放行该网段的 Ping：

```powershell
New-NetFirewallRule -DisplayName "Allow ICMPv4 from Race LAN" `
  -Direction Inbound -Protocol ICMPv4 -IcmpType 8 `
  -RemoteAddress "192.168.50.0/24" -Action Allow
```

如果当前窗口提示符为 `C:\...>`，说明使用的是 CMD，不能直接运行 PowerShell cmdlet。
可改用管理员 PowerShell，或在管理员 CMD 中运行：

```cmd
netsh advfirewall firewall add rule name="Allow ICMPv4 from Race LAN" dir=in action=allow protocol=icmpv4:8,any remoteip=192.168.50.0/24
```

复制命令时只复制代码框内容，不要复制 `PS C:\Users\...>` 或 `C:\Users\...>` 提示符。

若 `18765` 监听正常但远程端口测试失败，可在 FinishReview 电脑的管理员 PowerShell 中
只放行所需端口和赛事网段：

```powershell
New-NetFirewallRule -DisplayName "FinishReview 18765 from Race LAN" `
  -Direction Inbound -Protocol TCP -LocalPort 18765 `
  -RemoteAddress "192.168.50.0/24" -Action Allow
```

不要为了排障永久关闭整个 Windows 防火墙。

### IP 冲突检查

出现“本机赛事网卡已断开，但其他电脑仍能 Ping 本机固定 IP”时，优先怀疑另一台设备占用了
相同地址。先在目标电脑记录赛事网卡 MAC：

```powershell
Get-NetAdapter -Physical | Format-Table Name, Status, MacAddress
```

再在发起连接的电脑检查 ARP：

```powershell
arp -a | Select-String "192.168.50.10"
```

ARP 中的 MAC 必须与目标电脑赛事网卡一致。不一致时先断开重复设备并重新分配唯一 IP，
不要继续增加防火墙规则。

### 老款海康摄像机

老款海康摄像机可能把现代浏览器重定向到 `/notSupported.asp`，而设备固件又没有该页面，
最终显示 `404 Not Found`。这表示浏览器兼容性问题，不表示摄像机离线。优先使用海康
SADP、iVMS-4200 或 Edge IE 模式完成配置；官方工具无法支持过老设备时，应使用隔离的
兼容环境，不要把摄像机暴露到互联网。

普通主码流使用：

```text
rtsp://192.168.50.63:554/Streaming/Channels/101
rtsp://192.168.50.64:554/Streaming/Channels/101
rtsp://192.168.50.65:554/Streaming/Channels/101
```

账号和密码通过应用凭据字段输入，不写入手册、URL、日志或截图。RTSP 返回
`401 Unauthorized` 表示网络和 RTSP 路径已到达摄像机，但凭据认证失败；它不是交换机
速度问题。`101` 是主码流，`102` 通常是子码流，正式复核应在性能允许时保留主码流细节。

摄像机画面显示 `1970` 年时，在 iVMS-4200、SADP 或兼容网页中设置：

- 时区：`UTC+08:00`；
- 夏令时：关闭；
- 时间源：与赛事电脑同步，或统一使用同一个可信 NTP；
- 完成后：重启预览并核对三台摄像机、CycleRace 和高速摄像电脑的时间偏差。

普通网络摄像机用于全景录像和辅助复核，不能因交换机升级为千兆而视为高速过线摄像机。
正式验收仍需用真实过线事件确认 CycleRace 时间、普通录像和高速证据能够正确关联。

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
