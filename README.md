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

## 相机 1 连续判读

上方“时间胶卷”下沿显示与画面共用刻度的判读时间线：红色游标表示当前原帧，
号码标记按录像中的实际位置排列，滚动、调整间隔和大小时一起对齐。
点击号码直接回看保存的原帧；单机位模式会打开判读窗口。同帧或密集号码显示为
可展开的组合标记，每条记录仍可独立选择。“全部记录”展开整场判读记录列表，
切换运动员、筛选组别和切换录像文件后仍保留，重启后从赛事判读日志恢复。
胶卷、机位和记录统一显示校时后的判读时间；原始录像时钟和帧位置保持不变。
机位录像定位条从左向右推进时间，支持拖动；拖动中的请求合并预览，松开后精确定位。
日常操作是单击名单选择身份、左右方向键逐帧查看、标线后按 Enter 确认；
双击名单则定位该运动员的芯片时间。

时间胶卷用于浏览整场相机 1 录像、查找漏判，正式判定始终在机位 1。
上方整场时间条可点击定位，缩略图按录像时间从左到右排列，保留完整画面。
默认每 100 ms 一张，可切换 50、200、500 ms，以及 1、2、5、10 秒；较大间隔用于
快速查找时段，再缩小间隔查看密集到达和遮挡。拖动胶卷、滚轮或“前一屏 / 后一屏”
连续浏览。“当前帧”定位当前判读时间；换号码、重新确认和新录像归档不会
自动把胶卷拉回前面。胶卷仅解码可见区域及少量邻近图片，缓存有固定上限。
胶卷画面随区域高度放大，卡片宽度按原画面比例调整，保留完整画面。
可拖动胶卷与名单之间的分隔线调节高度，或在“更多”中选择“放大胶卷 / 还原布局”。
调整大小保持原浏览时间；缩略图最高保留 960×720，图片缓存限制为 64 MiB。

点击缩略图回到机位 1 的对应原始帧，不自动改变号码或确认判定；在那里核对身份、
手动标线并确认。名单中缺少的运动员可先在计时软件补录，再接收新记录后判读。
看到运动员所在的缩略图后先点击图片，再按 **F**，会保持这张原帧并直接放大机位 1；
放大后仍需人工核对身份、逐帧查看、标线和确认，不会自动选号码或判定。
单相机 1 模式下，主界面显示胶卷和名单，机位 1 仅在判读窗口中显示。
判读窗口带上同一份名单，可选号码、搜索、逐帧和确认；Esc / F 或“返回胶卷”关闭后，
名单恢复到主界面，胶卷保留原浏览时间。双击名单也可直接打开对应计时时刻判读。
胶卷不依赖名单、组别或已判状态，所以没有芯片记录的录像也可浏览。
录像分段按实际时间衔接，缺口保留时长并明确标出；解码失败的画面不可用于定位。
归档后自动扩展整场范围，未归档或没有可靠时间范围的录像显示等待状态。
已判号码在胶卷下沿按时间显示，完整记录可通过“全部记录”展开。

“本屏已检查”手动标记完整显示且已加载的缩略图所代表的时间范围，录像缺口、
不可用文件、加载失败和裁切在屏幕边缘的图片不计入；“更多 → 取消本屏检查”可撤销。
整场时间条下方的绿色显示检查范围，上方绿点显示已判记录，两者独立；
“下一处未检查”继续查漏，末尾没有剩余时回到前面的未检查范围。
进度写入赛事目录的 `filmstrip_checks.jsonl`，按赛事和相机隔离，重新打开后恢复。
选择运动员或确认判定不会自动标记任何时间范围已检查。

“跟随最新”持续定位最新可回看的录像；拖动、翻页或点击原帧会退出跟随。
查漏期间归档的新录像只追加到末尾并提示新增时长，保持当前浏览位置；
尚未归档且没有可靠时间范围的录像继续等待，不当作实时画面显示。

同一录像时刻的记录沿用名单顺序显示；重判保持记录条的滚动位置，未勾选
“确认后下一条”时保留当前运动员。

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
