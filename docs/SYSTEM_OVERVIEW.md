# FinishReview 系统详细说明

> 适用版本：v0.25.6（代码位于 `realtime` 包，入口 `python -m realtime.review_main`）。
> 本文按“现场怎么用 → 数据怎么流 → 每层怎么实现 → 文件放在哪 → 哪些事不做”的顺序说明。

## 0. 一句话定位

FinishReview 是一套**独立的自行车比赛终点复核台**：它一边接收 CycleRace（或赛虎）的芯片过线事件，
一边录制终点摄像头；用视频自动找出“芯片与画面可能对不上”的选手，只把对不上的问题交给人判断；
最后把补录、建议删除、建议改号改圈三张清单导出给计时软件。

它**不是计时系统**，也不生成或修改正式成绩。正式成绩永远以 CycleRace 的芯片时间为准。

---

## 1. 定位与硬边界

这些边界决定了整个系统的设计，读代码前应先记住：

| 边界 | 含义 |
| --- | --- |
| CycleRace 是唯一权威 | 芯片时间是正式成绩；视频只用于发现问题、提供证据和漏读补录时间 |
| 不识别号码 | 不做 OCR、不做号码牌识别；号码由操作员看原帧后录入 |
| 不自动改成绩 | 所有处理只写本地日志，导出 CSV 交给 CycleRace 人工执行 |
| 自动通过 ≠ 人工确认 | 芯片与画面自动配对成功的选手单独记录，不冒充人工确认 |
| 软件要分发 | 不使用 AGPL 组件；不依赖 Ultralytics / Torch / ONNX Runtime / Paddle |
| 认车可选 | 使用者自行配置 Apache-2.0 的 YOLOX ONNX；通过 OpenCV `cv2.dnn` 单线程运行；模型不随软件分发 |
| 与 CycleRace 同机 | 分析必须让路：低优先级、可暂停、可晚出结果；内存低于门槛不开始 |
| 多圈赛只判最后一圈 | 圈数异常只提示，不逐圈做视频判读 |

核心判断顺序是：**本机能自动对上的不打扰人；对不上的才进问题清单；本机定不了的由人决定。**

---

## 2. 总体数据流

```text
CycleRace ──HTTP JSONL──┐
（或赛虎轮询）           │
                        ├──> 赛事目录（只追加日志）
摄像头/RTSP/USB ─FFmpeg─┤      ├─ cyclerace_passage_events.jsonl
                        │      ├─ video_timeline.jsonl
奥亚特高速相机文件 ──────┘      └─ video_clock_models.jsonl
                                        │
                              读日志 + 录像 + 终点线
                                        │
                     ┌──────────────────┴──────────────────┐
                     │ 视频分析（独立低优先级进程）          │
                     │ 线取样 → 过线检测 → 校时 → 配对      │
                     │ → 可选 YOLOX 认车 → 合并规则          │
                     └──────────────────┬──────────────────┘
                                        │
                              finish_problems/problem_list.json
                                        │
                     ┌──────────────────┴──────────────────┐
                     │ 问题清单界面：左队列 / 中原帧 / 右处理 │
                     │ 底部整场时间胶卷                      │
                     └──────────────────┬──────────────────┘
                                        │ 操作员决定
                              problem_resolutions.jsonl
                                        │
                          导出三张 CSV → CycleRace 执行
```

另外有一条并行的“人眼链路”：时间胶卷 + 通过记录 + 原帧判读。它不依赖分析结果，
用于复查、定位漏判和为问题条目提供身份/原帧证据。

---

## 3. 计时数据接入

### 3.1 CycleRace 链路（默认）

实现：`realtime/passage_receiver.py`。

- **HTTP 接收服务**：默认监听 `0.0.0.0:18765`，路径 `/api/v1/passage-events`。
- **局域网发现**：UDP `18766`，请求字面量 `CYCLERACE_DISCOVER_VIDEOPIPE_V1`，
  回复 `videopipe-finish` 服务和 HTTP 端口，避免现场手工配 IP。
- **协议 v1 事件**（`PassageEvent`）主要字段：
  - `event_id`、`race_id`、`stage_id`、`group_id`、`sequence`
  - `chip_id`、`bib`、`passage_time_ms`、`lap`
  - `revision`、`is_active`、`passage_timestamp_ms`
  - 名单信息：`race_name`、`stage_name`、`group_name`、`athlete_*`、`team_name`
  - `received_at_ms`（本机接收时间，参与日期对齐但不作为过线时间）
- **只追加日志**：事件写入 `cyclerace_passage_events.jsonl`；同一个 `event_id` 保留最新
  `revision`。同一修订号内容冲突会报错，而不是静默覆盖。
- **先落盘再回调**：接收线程先持久化，再通知界面；避免界面崩溃导致数据丢失。
- **作废**：`is_active=false` 是上游墓碑；本地“删除记录”另存
  `cyclerace_passage_events_deleted.json`，绑定号码、芯片、时间，上游记录一改就重新出现。
- **本地删除不删除原始日志**：删除是带原因的本地标记，可以审计。
- **赛事焦点**：`race_focus` 消息让 CycleRace 选中某位运动员时，FinishReview 切到对应记录。
- **赛事元信息**：`cyclerace_race_metadata.json` 保存赛事、组别、名单快照，用于“名单外号码”“应到未到”等检查。
- **收件箱**：主窗口可能正在看历史赛事，实时数据先进入保存根目录的
  `.finishreview/` 收件箱，不打断当前画面；恢复现场时再合并。
- **安全边界**：当前 CycleRace 兼容链路没有身份认证，只能部署在受信任的赛事局域网。

### 3.2 赛虎（RaceTiger）链路

实现：`realtime/racetiger_source.py`、`realtime/receiver_controller.py`。

- 只读轮询远程接口，拿到过线事件后写入
  `racetiger_passage_events.jsonl`。
- 令牌使用 Windows DPAPI 加密保存；远程地址必须 HTTPS，只有 `localhost`/回环允许 HTTP。
- 与 CycleRace 链路二选一，由设置中的 `timing_provider` 决定。

### 3.3 赛后对账

`realtime/sync_check.py` 可以把 CycleRace 自己的数据库副本与 FinishReview 收到的日志对比，
发现“补录没同步过来”“作废没同步过来”等两边记录不一致的问题。

---

## 4. 视频接入与录像

实现：`realtime/review_recorder.py`、`realtime/stream_recorder.py`、
`realtime/recording_controller.py`、`realtime/video_timeline.py`。

### 4.1 输入源

- 主摄像头（机位 1）和可选副摄像头（机位 2）。
- 支持 RTSP 和 Windows USB/UVC（DirectShow）两种源。
- 奥亚特/AYT 高速线阵相机：只读扫描原厂数据目录，按日期和赛事定位文件，
  在 `auyat_rgb.py` 中解码；不复制、不改写。
- 第三方带时间戳的片段可以通过 `external_clip_import.py` 导入时间线。

### 4.2 录像策略

每个机位一个 FFmpeg 进程，用 `tee` 同时输出两路：

| 输出 | 位置 | 分段 | 用途 |
| --- | --- | --- | --- |
| 长归档 MKV | `videos/camera_XX_<session>_archive_%04d.mkv` | 默认 300 秒 | 赛后完整回放、问题证据 |
| 短段 HLS | `review_buffer/camera_XX/*.ts` + `.m3u8` | 默认 2 秒 | 比赛中近实时回看、问题分析 |

- 短段滚动保留约 360 秒；被通过记录或问题证据引用的短段会“钉住”保留。
- 短段封存后才发布，不读取仍在写入的文件。
- `video_timeline.jsonl` 记录每段的 `segment_id`、机位、路径、墙钟开始时间、
  媒体时长、结束原因、校时误差。
- 问题分析保留短段作为证据，**不用长归档替换它们**，所以磁盘要按两份录像预留。

### 4.3 定位原帧

- 取帧一律先跳到目标前约 4 秒，再顺序解码到目标帧；避免 H.265 按时间跳转不准。
- 界面和时间胶卷只使用实际解码出来的帧时间。
- 原帧引用包含：机位、`segment_id`、文件路径、帧号、片内毫秒位置、校时版本，
  这样归档生成后引用仍然稳定。

---

## 5. 时间模型（整个系统最关键的一层）

实现：`realtime/clock_sync.py`、`realtime/evidence_clock.py`、`realtime/time_domain.py`。

### 5.1 三个时间必须分开

| 时间 | 来源 | 用途 |
| --- | --- | --- |
| 芯片时间 | CycleRace | 正式成绩，永不修改 |
| 录像时间 | 录像电脑墙钟 | 定位文件和帧 |
| 显示/判读时间 | 校时后的时间 | 界面和证据展示 |

偏移只换算一次；证据定位到稳定的源录像位置。

### 5.2 自动校时

1. **粗校**：把所有“过线时刻 − 芯片时刻”的差值在 ±90 秒内按 100 ms 分箱，取主峰。
   三场真实数据都能得到单一明显峰。
2. **细校 + 漂移**：只取前后 5 秒内没有别人的“单独过线者”，稳健拟合

   ```text
   偏移 = a + b × (芯片时间 − 参考时刻)
   ```

   用中位数绝对偏差迭代剔除离群点；`b` 就是录像电脑时钟相对芯片时钟的漂移。
3. **会话修正**：同一机位多次重启录像时，每个会话可能有不同的固定延迟；
   会话中位数偏离共用漂移线超过阈值时，单独修正并提示核对。
4. **版本化**：每次重新拟合生成新的 `ClockModel`，写入 `video_clock_models.jsonl`；
   已处理的问题保留它当时使用的版本。

三场回放中的漂移约为每小时 −0.3 到 −0.9 秒，扣除漂移后的残差中位数约 27–29 ms。

### 5.3 人工锚点

- 第一位能明确对应的选手压线时，操作员点“以当前帧校时”，保存机位、会话、原帧和偏移。
- 人工锚点不是无条件生效：它绑定的芯片记录必须仍然存在、仍然有效、
  revision 和原始芯片时间都没变；否则锚点失效并提示重新核对。
- 自动结果与人工锚点相差超过 300 ms 时提示核对，不静默覆盖人工锚点。
- 自动校时还没成功时，芯片只能按“接收日期”定位参考帧，标记
  `reference_only`，不参与配对，也不当作已确认过线。

### 5.4 日期对齐

- 赛事范围由 `race_id` + `stage_id` 决定，赛事元信息里的日期不用于过滤芯片。
- 收到刷卡时，如果时分秒与接收时间吻合，可以用接收日期推断整数天偏移，
  支持跨年、提前测试、赛后测试和午夜边界。
- 日期映射只是定位线索，不是匹配成功的证据；多个日期都吻合时明确报告歧义。
- 配对、补录建议和证据定位使用同一套日期映射，避免把测试日期写进正式计时建议。

---

## 6. 视频分析流水线

实现：`realtime/problem_analysis.py`，各阶段都落盘缓存，中断后可续跑。

### 6.1 阶段总览

```text
1. 线取样       每帧沿终点线取一条像素 → strips/*.npz
2. 过线检测     线取样图 → 过线事件 + 遮挡时段 → crossings_cameraN.json
3. 校时         过线时刻 vs 芯片时刻 → 漂移模型 → video_clock_models.jsonl
4. 配对         过线序列 vs 芯片序列 → 一对一匹配
5. 认车（可选） 只对没配上的过线截图跑 YOLOX
6. 合并规则     配对结果 → 问题条目 + 过线卡片 → problem_list.json
```

### 6.2 线取样

`realtime/line_sampling.py`：

- 在配置的终点线上取约 500 个采样点，每点取 4 像素宽的均值。
- 每帧贡献一列，按时间拼成“线取样图”（行 = 线上位置，列 = 帧）。
- 不跑模型，只解码和取像素，CPU 占用低。
- 结果以压缩 `.npz` + JSON 元数据保存；缓存键包含终点线指纹、采样数、文件大小、
  `segment_id`、排除区间，参数或文件变化才重算。

### 6.3 过线检测

`realtime/crossing_detection.py`：

- 每个线上位置取前后约 ±3 秒的中位数作为背景，并扣除整帧亮度变化。
- 与背景的颜色差超过阈值（默认 40）的连通区域作为候选。
- 同一人的碎片按“起点相近（150 ms）”或“仍在原位置”合并。
- “像骑手”的条件：遮挡高度至少约 20 行（约线长的 4%），持续不超过 1 秒。
- 更长遮挡记为“线被挡/拥挤时段”；遮挡内部短促的剧烈变化保留为“过线候选”，
  只用于芯片配对，不单独报“无芯片”。
- 参数以毫秒表示，按实际帧间隔换算，20/25 fps 行为一致。

### 6.4 目标轨迹复核（配置模型时）

`realtime/target_crossing.py`：

- 不是“像素变了就算过线”，而是检查一个目标框的底部中心是否真的从线的一侧到了另一侧。
- 只在终点线附近、按少量偏移帧检查；模型失败时保留线事件待核，不自动配对。

### 6.5 配对

`realtime/crossing_pairing.py`：

- 把“过线序列”和“芯片按校时模型映射到录像时间的序列”做保序一对一动态规划对齐。
- 配对成本 `0.5 × (残差 / 150 ms)²`；跳过任一侧的固定成本 `1.6`；
  只有残差在 ±700 ms 内才允许配对。
- 时间轴上有大于窗口的空隙就切块，长比赛仍然很快。
- 关键点：**从不把“最近的芯片”直接认给一次过线**；每一对都要和“双方都跳过”竞争。

### 6.6 认车（可选）

`realtime/bike_check.py`：

- 使用者配置 YOLOX ONNX 路径；OpenCV `cv2.dnn` 单线程推理。
- 只对配不上的过线取 1–3 帧截图检测，不是逐帧跑。
- 类别：自行车、行人、摩托车/汽车/卡车等。
- 只有高把握的非自行车目标才建议排除；画面边缘被切、几帧结论不一致、
  拥挤时段一律标“有争议”，留在清单里由人决定。
- 认车只回答“是不是骑车冲线”，不回答“是谁”。

### 6.7 合并规则

`realtime/problem_rules.py`，按顺序：

1. 没配上的过线与附近的“只有芯片”记录，在 0.8 秒内（双方都孤立时放宽到 1.5 秒）
   视为迟到芯片，自动通过；
2. 已配对芯片旁边多出来的过线，只有“同一时刻（150 ms）或同一会话相邻两帧”
   才合并为同一次过线；否则生成“附近可能另有过线”候选，绝不静默丢弃；
3. 同一位置 90 秒内反复出现 3 次以上活动，合并为“有人停留”；
4. 剩余像骑手的过线 → “有人过线，没有芯片”；
5. 只有芯片、附近没有过线 → “有芯片，没看到过线”；附近有已配对过线 → “一次过线，两个芯片”；
6. 4 人以上、相隔 2 秒以内，或视频过线数多于芯片数的小集团 → 集团核对；
   长时间遮挡 → 遮挡/拥挤时段条目。

### 6.8 输出

`finish_problems/problem_list.json` 包含：

- `items`：问题条目；
- `auto_passed`：自动配对成功的过线记录（不是人工确认）；
- `summary`：自动通过、待处理、有争议、已上游关闭、已排除、无法核实等计数；
- `cameras`：每个机位的覆盖率、分析水位、校时警告、认车状态；
- `passage_members`：芯片记录快照，供批次成员级处理。

同时生成 `finish_problems/strips/` 和 `finish_problems/crossing_cards/`（过线卡片截图）。

### 6.9 实时与离线两种运行方式

- **实时**：独立低优先级进程，每轮只处理新封存的短段，结果原子替换发布；
  默认保留约 10 秒证据等待区，等待区内不下漏读结论；没有新录像/芯片/配置变化不重复启动；
  每轮结束至少等 10 秒；集团核对期间暂停。
- **离线**：`--analyze-problems <赛事目录>`，可整场跑，也可只跑到线取样；
  可用内存低于门槛（默认 1536 MB）直接跳过并说明原因；内存按阶段记录峰值。

---

## 7. 芯片自检（不需要视频）

实现：`realtime/chip_checks.py`、`realtime/chip_history.py`。

实时就能跑，检查项包括：

| 检查 | 条件/说明 |
| --- | --- |
| 测试/备用芯片 | 组名属于测试组、备用组，不应出现在比赛读数中 |
| 名单外号码 | 号码不在报名名单 |
| 同号重复读数 | 10 秒内同号/同芯片多次读数 |
| 重复补录 | 同一记录被多次补录 |
| 同号多次有效通过 | 非多圈赛出现多次有效通过 |
| 成批读数 | 1 秒内成批，或 200 ms 内 6 条以上 |
| 早于发枪 | 需要各组发枪时间 |
| 发枪后过早 | 按距离和最大速度估算不可能这么早到达 |
| 圈号重复/倒退/单圈异常/末圈缺读 | 多圈赛 |
| 应到未到 | 名单减去已到、退赛、OTL、DNS |
| 两边记录不一致 | CycleRace 数据库副本与接收日志对比 |

这些检查只产生“疑点”，不改变成绩。CycleRace 后续作废或修改记录时，
条目保留但标记“上游已关闭”，既能看到抓到过什么，也能看到已经修好什么。

---

## 8. 问题清单：操作员的主界面

实现：`realtime/problem_panel.py`、`realtime/review_window.py`、`realtime/problem_items.py`。

### 8.1 布局

```text
┌──────────────────────────────────────────────────────────────┐
│ 接入 / 录像 / 取样 / 校时 / 分析 / 证据缺口（各自独立状态）      │
├──────────────────────────────────────────────────────────────┤
│ 无需人工处理 N 人 │ 多芯片 │ 集团 │ 漏读 │ 记录与圈数 │ 待证据   │
├───────────────┬──────────────────────────┬───────────────────┤
│ 左侧问题队列   │ 中间原帧 / 过线卡片       │ 右侧处理与证据      │
│ 待处理/待证据/ │ 前后约 1 秒小胶片         │ 类型入口 + 决定按钮 │
│ 已处理/全部    │ “跳到原帧”                │ 号码输入 + 原因     │
├───────────────┴──────────────────────────┴───────────────────┤
│ 底部整场时间胶卷（缩略图、缺口、已判记录、检查覆盖）             │
└──────────────────────────────────────────────────────────────┘
```

### 8.2 条目分类

机器分类（`kind`）有二十多种，但操作员只看四个类：

| 操作员类 | 包含的典型问题 |
| --- | --- |
| 一人双芯片 | 一次过线两个芯片、重复读数、测试/备用芯片、成批读数 |
| 集团 | 集团核对、线被挡/拥挤、有人停留 |
| 漏读 | 有人过线没芯片、附近可能另有过线、有芯片没看到过线、人工问题 |
| 待确认 | 等待录像/校时，以及不属于上面三类的其他疑点 |

正常自动对应的单人**不占待处理**，只在“全部”里按时间顺序出现，供复查和补记。

### 8.3 状态

`ProblemItem` 的状态：

- `open`：待处理；
- `disputed`：有争议；
- `closed_upstream`：CycleRace 已作废/修改，自动关闭；
- `excluded`：已排除（如非选手）；
- `info`：记录性条目（如自动通过）；
- `waiting`：等待录像或校时。

`pending` 只包含 `open`/`disputed` 且不属于某个集团子项。

### 8.4 条目身份

`item_id` 由问题类型和底层记录（`event_id`、机位、`segment_id`、帧号等）哈希得到。
只要底层记录不变，重新分析不会换 ID，操作员的草稿和处理记录不会丢。

### 8.5 操作按钮

按钮就是问题本身，不需要先选原因再确认：

- “是选手”：有号码就补录；号码看不清就确认过线、待认号；
- “不是选手”：行人、工作人员、车辆；
- “不是完赛过线”：往回骑或推车；
- “同一次过线”：后轮/影子；
- “留 020 / 留 023”：一次过线两个芯片，选择保留哪条；
- “两个人”：并排两人；
- “建议删除”“建议改号改圈”“看不清，先放着”“已在 CycleRace 处理”。

需要依据的决定会带一条预填原因，仍可编辑；同帧同位置确认第二个号码会提示冲突
（定边 067/057、051/029 就是这么错的）。

### 8.6 处理记录与防错

`problem_resolutions.jsonl` 只追加，每个条目保留最新 revision。每条决定绑定：

- 号码、`event_id`、芯片时间；
- 证据帧（机位、文件、帧号、片内位置）；
- 校时版本；
- 分析签名。

上游记录一变（改号、改时间、作废、新 revision），决定就“过期”，条目重新要求处理；
“重新处理”可以撤回本机决定，但不会撤销 CycleRace 里已经做的修改。

### 8.7 草稿、暂存与人工问题

- `operator_drafts.json`：每条问题的输入、点数、已看范围、暂存原因和人工处理快照。
- “暂存”表示等裁判/等证据，不是已解决。
- 从胶卷原帧可以“添加人工问题”，必须写原因，保存视频、机位、帧号和位置；
  不直接断言缺芯片，也不生成成绩。
- 浏览其他时段会保留当前问题和输入，但锁定提交按钮；回到当前问题并等原帧到达后才能提交。

### 8.8 导出

`realtime/problem_resolutions.py` 的 `export_lists()` 生成三张 UTF-8 BOM CSV：

| 文件 | 内容 |
| --- | --- |
| `补录清单.csv` | 号码、芯片域时间、证据帧、机位、校时版本、说明、时间来源 |
| `建议删除清单.csv` | 号码、通过时间、CycleRace 记录编号、问题类型、原因、事件 ID、证据快照 |
| `建议改号改圈清单.csv` | 原号码、建议号码/圈数、通过时间、记录编号、原因 |

在 CycleRace 处理完后，操作员明确点“已在 CycleRace 处理”，本机才认为闭环。

---

## 9. 集团逐帧核对

实现：`realtime/bunch_dialog.py`、`realtime/bunch_review.py`、`realtime/bunch_timeline.py`。

- 入口是集团条目上的“打开集团逐帧核对”，在独立的集团窗口里完成，不需要另开一套流程。
- 上方是集团自己的时间尺：原始过线图片带、芯片刻度、检测到的每个人。
  蓝线 = 画面检测到的过线；红线 = 芯片预期时刻；绿线 = 已点数的人；灰线 = 已排除。
- 操作员在画面上逐人点击；每次点击绑定当时那一帧和位置。
- **人数等于芯片条数的那一刻自动完成并保存**，不需要再按确认。
- 只有两条硬门槛：本团录像存在真实缺口时不能“完整核对”；同一号码被填在多人上要先核对。
- 补录时间按集团内前后两人的芯片时间插值，不只用偏移换算，避免集团内名次错乱。
- 草稿、点数、已看范围写入 `bunch_reviews.jsonl`，重开恢复，不重新开始。
- 多芯片批次按成员逐条处理，写入 `batch_member_decisions.jsonl`；
  批次身份写入 `batch_identity_snapshot.json`，重新分析不换批次 ID。

---

## 10. 人工判读与时间胶卷（保留的复核链路）

实现：`realtime/passage_review.py`、`realtime/race_filmstrip.py`、
`realtime/passage_evidence.py`、`realtime/video_playback.py`。

这条链路不依赖问题分析，主要用于：

- 复查自动结果；
- 给问题条目确认身份和原帧；
- 查找分析没发现的漏判；
- 观看没有芯片记录的录像。

典型操作：

1. 第一位能明确对应的选手压线时，点“以当前帧校时”，选择其有效芯片记录；
2. 在名单里单击选择身份，左右方向键逐帧查看，标线后按 Enter 确认；
3. 双击名单定位该运动员的芯片时间；
4. `F` 在原地放大画面，右侧仍是同一张过线表；
5. 胶卷默认 500 ms 一张，可切 50/100/200 ms 或 1/2/5/10 秒；
   滚轮上下浏览，拖动定位，点击缩略图回到机位 1 原帧；
6. “本屏已检查/取消本屏检查”写入 `filmstrip_checks.jsonl`；
7. 确认结果写入 `passage_evidence_associations.jsonl`，绑定通过记录、机位、片段和原帧位置。

判读永远在机位 1；胶卷只负责浏览和定位，不自动改变号码或确认判定。

---

## 11. 现场模式与生命周期

主窗口有两种工作模式：

- **现场（live）**：有录像、实时接收芯片；
- **历史查看（archive）**：在看已保存的赛事，不接收现场画面打断。

关键行为：

- **开始录像不依赖芯片**：设备空闲、还没有任何通过记录时也能先录；
  胶卷范围切到“本次录像”，旧录像仍可手动切到“全部录像”。
- **返回现场**：优先接回独立收件箱里已收到的赛事，其次是当前已选赛事；
  两处都没有时，在保存根目录下新建隔离的“现场录像_时间戳”目录，不写进无关历史赛事。
- **历史查看期间**：实时数据只进收件箱，不打断正在看的历史画面。
- **清空数据**：删除本场通过记录、问题判读、校时和试录画面，直接删除、不保留副本；
  保留终点线、认车模型、摄像头和保存位置；CycleRace 正式成绩不受影响；
  录像中或看历史时不可用。
- **状态分开表达**：接入、录像、取样、校时、分析各自报自己的原因；
  “证据缺口”把“还在等证据的条目”和“没有录像可核的芯片数”分开写；
  未分析、等待录像/校时、分析到某时刻且当前无待处理，三种状态不互相冒充。
- **停止录像后**：补做末轮分析，收尾证据等待区；手动暂停分析不停止录像。

---

## 12. 文件与目录

### 12.1 程序配置与日志

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\FinishReview\config.json` | 摄像头源、保存目录、计时源、赛虎令牌（DPAPI 加密）等 |
| `%LOCALAPPDATA%\FinishReview\logs\finish_review.log` | 轮转日志，最多 5 个历史文件，每个 2 MB |

### 12.2 赛事目录（一个赛事/赛段一个目录）

| 文件/目录 | 内容 |
| --- | --- |
| `cyclerace_passage_events.jsonl` | CycleRace 过线事件只追加日志（最新 revision 生效） |
| `cyclerace_passage_events_deleted.json` | 本地带原因的删除标记 |
| `cyclerace_race_metadata.json` | 赛事、赛段、组别、运动员名单快照 |
| `video_timeline.jsonl` | 录像分段与墙钟时间映射 |
| `video_clock_calibrations.jsonl` | 人工首人校时锚点 |
| `video_clock_models.jsonl` | 自动校时模型版本 |
| `passage_evidence_associations.jsonl` | 人工确认的“通过记录 ↔ 原帧”关联 |
| `review_clips.jsonl` | 共享回看片段与事件绑定 |
| `finish_lines.json` / `motion_finish_lines.json` | 终点线几何（当前用后者，兼容前者） |
| `long_image_lines.json` | 线阵相机/长图终点线几何 |
| `review_buffer/camera_XX/` | 2 秒 HLS 短段、播放列表、钉住记录 |
| `videos/` | 300 秒长归档 MKV 和 `archive_session.json` |
| `finish_problems/problem_list.json` | 当前问题清单与分析水位 |
| `finish_problems/strips/` | 线取样缓存 |
| `finish_problems/crossing_cards/` | 过线卡片截图 |
| `finish_problems/analysis_config.json` | 模型路径、采样配置、内存门槛等 |
| `finish_problems/operator_drafts.json` | 草稿、暂存、点数、已看范围 |
| `finish_problems/manual_problems.json` | 人工发现的问题 |
| `finish_problems/problem_resolutions.jsonl` | 操作员决定日志 |
| `finish_problems/live_chip_checks.json` | 实时芯片自检快照 |
| `finish_problems/bunch_reviews.jsonl` | 集团核对记录 |
| `finish_problems/batch_member_decisions.jsonl` | 多芯片批次成员决定 |
| `finish_problems/batch_identity_snapshot.json` | 批次身份，重新分析不换 ID |
| `finish_problems/near_crossing_history.jsonl` | “附近可能另有过线”候选的发现/关闭历史 |
| `finish_problems/exports/` | 三张交接 CSV |
| `filmstrip_checks.jsonl` | 胶卷人工检查覆盖 |
| `video_review.jsonl` / `arrival_reviews.jsonl` | 视频异常/到达候选的人工处理记录 |
| `preflight_tests.jsonl` | 赛前设备测试记录 |
| `review_event_scope.json` | 当前赛事范围 |
| `<保存根目录>/.finishreview/` | 实时数据收件箱 |

---

## 13. 资源、可靠性与安全

### 13.1 资源优先级

```text
CycleRace 计时 > 录像写盘 > 当前处理画面 > 线取样 > 认车 > 其他分析
```

- 分析进程降低优先级并请求 Windows 效率模式；CPU 繁忙时让步。
- OpenCV 线程限制为 1；每个机位最多 2 个解码线程。
- 可用内存低于 1536 MB（可配置）不开始分析。
- 不直接打开 CycleRace 的 `race.db`，避免与计时写入争锁；赛后用副本。
- 录像写盘前检查剩余空间，少于 1 GB 不允许开始录像。

### 13.2 数据可靠性

- 日志采用“追加 + fsync + 原子替换”的写法；JSON 快照先写临时文件再 `os.replace`。
- 尾部不完整行：可恢复的日志会忽略最后半行；只读归档不做尾部修复，打开不写盘。
- 决定绑定内容而不是只绑定 ID；上游记录改变时自动过期，避免错误隐藏新记录。
- 新分析结果替换问题列表时，已处理快照和草稿保留；正在看的问题被替代时锁定决定并提示选择新条目。
- CycleRace 数据晚到：证据等待区之后仍会重新核对，等待区不是网络时延保证。

### 13.3 安全

- 赛虎令牌、RTSP 密码使用当前 Windows 用户的 DPAPI 加密；复制配置到别的用户/电脑需重新输入。
- 赛虎远程地址必须 HTTPS；CycleRace 链路暂无认证，只能用于受信任局域网。
- 运行日志自动轮转，避免无限增长。

---

## 14. 部署与开发

- Python ≥ 3.10，依赖 `numpy`、`opencv-python`、`PyQt5`。
- 需要可用的 FFmpeg：放在程序目录、加入 `PATH`，或用 `FINISH_REVIEW_FFMPEG` 指定。
- 开发：

  ```powershell
  python -m venv .venv
  .\.venv\Scripts\python -m pip install -e ".[dev]" -c constraints-dev.txt
  .\.venv\Scripts\python -m ruff check realtime tests tools
  .\.venv\Scripts\python -m pytest -q --basetemp .pytest_tmp_finish_review
  .\.venv\Scripts\python -m realtime.review_main
  ```

- 打包：`.\packaging\build.ps1`，输出 `artifacts\dist\FinishReviewConsole`；
  打包显式排除检测、OCR、模型框架，并检查没有混入比赛数据、日志和本机配置。
- 打包后冒烟检查会创建 Qt 窗口、验证可见性，然后退出。
- 启动参数中 `--source` / `--usb-device` 是安装人员配置录像源用的；
  `--install-source` 保存后退出；`--analyze-problems` 等是无界面的后台入口。

---

## 15. 设计取舍与已知限制

- **不识别号码**：号码牌常被挡住，OCR 不可靠；系统数人数、找疑点，身份由人确认。
- **自动通过不是人工确认**：它是“芯片和画面按时间对上了”的记录，复查时仍可打开原帧。
- **密集集团自动计数不可靠**：并排相差 20–30 ms 的骑手会被合并成一次过线；
  4 人以上集团必须逐帧点数。
- **遮挡和录像缺口只能如实报告**：系统不补造证据，也不把“没看到”当成“没有”。
- **参数来自三场真实比赛**：定边、同心、贺兰山；第四场应做盲测。
- **两套录像同时占盘**：短段保留作证据，长归档也保留，磁盘按两份预留。
- **没有模型时清单更长**：认车只是降噪，不是必需；未配置模型不排除任何条目。
- **实时结果有等待区**：芯片晚于等待区到达时，后续分析会重新核对，但不能保证所有网络时延。
- **界面观感仍需现场走查**：离屏几何测试只能验证不越界，真实字体和系统缩放下的效果要人工看。

---

## 16. 模块地图

| 领域 | 主要模块 |
| --- | --- |
| 入口/主窗口 | `review_main.py`、`review_window.py`、`launch_dialog.py` |
| 问题清单 UI | `problem_panel.py`、`problem_items.py`、`crossing_widgets.py`、`crossing_cards.py` |
| 集团核对 | `bunch_dialog.py`、`bunch_review.py`、`bunch_timeline.py` |
| 人工判读/回放 | `passage_review.py`、`passage_evidence.py`、`race_filmstrip.py`、`video_playback.py`、`point_playback.py`、`playback_coordinator.py` |
| 计时接入 | `passage_receiver.py`、`racetiger_source.py`、`receiver_controller.py`、`race_metadata.py`、`sync_check.py`、`chip_history.py` |
| 录像 | `review_recorder.py`、`stream_recorder.py`、`recording_controller.py`、`video_timeline.py`、`review_buffer_journal.py`、`review_clip.py` |
| 分析 | `problem_analysis.py`、`line_sampling.py`、`crossing_detection.py`、`crossing_pairing.py`、`clock_sync.py`、`problem_rules.py`、`target_crossing.py`、`bike_check.py`、`chip_checks.py` |
| 结果与交接 | `problem_resolutions.py`、`batch_review.py`、`review_export.py`、`candidate_history.py` |
| 工作区/数据 | `analysis_workspace.py`、`event_workspace.py`、`workspace_context.py`、`workspace_consistency.py`、`durable_jsonl.py`、`time_domain.py` |
| 资源/安全/诊断 | `analysis_resources.py`、`decode_resources.py`、`secure_storage.py`、`runtime_status.py`、`runtime_paths.py`、`preflight.py` |
| 高速相机/外部片段 | `auyat_rgb.py`、`external_clip_import.py` |
| 视频辅助候选 | `visual_crossing.py`、`video_passage_detector.py`、`video_arrival.py`、`video_review.py` |

---

## 17. 最短阅读路径

如果只想快速理解实现，建议按这个顺序读：

1. `README.md` 和 `docs/LIVE_PROBLEM_WORKBENCH.md` —— 现场流程和界面语义；
2. `docs/FIELD_CROSSING_WORKFLOW.md` —— 先确认过线、再核对芯片的操作闭环；
3. `realtime/problem_analysis.py` 顶部注释 + `analyse_camera()` —— 分析主线；
4. `realtime/clock_sync.py` 的 `calibrate_camera()` —— 时间模型；
5. `realtime/problem_rules.py` 顶部注释 + `build_video_items()` —— 问题是怎么来的；
6. `realtime/problem_items.py` —— 条目分类、按钮和状态；
7. `realtime/problem_resolutions.py` —— 决定如何落盘、如何导出；
8. `realtime/review_window.py` + `problem_panel.py` —— 界面如何把这些串起来；
9. `realtime/review_recorder.py` + `video_timeline.py` —— 录像和原帧定位。

---

## 18. 一句话总结

**CycleRace 负责“谁什么时候过了线”，摄像头负责“画面里到底发生了什么”，
FinishReview 负责把两者按时间对齐，只把对不上的部分交给人，并把人的判断整理成
计时软件可以执行的建议。**
