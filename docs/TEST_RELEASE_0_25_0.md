# 0.25.0 测试版：终点问题清单

2026-10-15。从 `cocofee/finish-problem-list` 的 `ca3ac6d` 继续，按
[交接待办](HANDOFF_NEXT_STEPS.md) 第 2、3 项准备测试版。使用本分支原有的问题清单、
分析流程、处理记录和导出格式。

## 本次变更

- 分析启动的可用物理内存门槛默认由 4096 MB 改为 1536 MB。
  `finish_problems/analysis_config.json` 的 `min_available_mb` 和赛后工具
  `--min-free-mb` 可覆盖默认值；已有自定义值保持有效。
- 项目版本、主界面版本和 Windows EXE 的文件 / 产品版本统一为 0.25.0。
- 修复 Windows 打包后的中文分析进度：进度协议使用转义 JSON，接收端按完整行解析，
  保留跨读取分块的消息，并在进程结束时读取剩余输出。界面仍显示中文。
- 分析仍在独立低优先级进程中运行，解码和认车线程限制沿用交接实现。

## 打包与检查

构建使用项目原有虚拟环境及 FFmpeg：

```powershell
.\packaging\build.ps1 `
  -PythonPath D:\orca\FinishReview\.venv\Scripts\python.exe `
  -FfmpegPath D:\orca\FinishReview\artifacts\release-0.24.9\FinishReviewConsole\_internal\ffmpeg.exe
```

本机验证日志和合成数据位于 `artifacts/handoff-0.25.0/`；不会进入发布包。
检查包括完整回归、Ruff、编译、发布目录边界、EXE `--smoke-test`、EXE 合成录像分析，
以及经真实 QProcess 读取 EXE 的中文内存不足提示。

- 最终连续回归：986 项通过、1 项跳过，耗时 258.80 秒，日志 `pytest-verified.log`。
- Ruff、编译和 `git diff --check` 通过。
- EXE 冒烟退出码 0，耗时 1753 ms；文件及产品版本均为 0.25.0。
- 最终 ZIP 解压后在隔离配置目录启动通过，退出码 0，耗时 3017 ms；EXE 哈希与构建产物一致。
- EXE 经 QProcess 传回的内存不足提示保持完整中文，没有替换字符。
- 发布目录边界检查通过，递归扫描没有比赛录像、芯片记录、数据库和模型权重。

合成录像包含 14 次过线，删掉一条芯片读数并添加一条没有过线的芯片：
实际自动通过 13 人、待处理 2 条（漏读和多余芯片各一条），且两条都有原帧卡片和
9 格小胶片。输入文件保持不变；EXE 退出码 0，耗时 1.69 秒。

## 定边演练

本机演练入口：`artifacts/release-0.25.0/run_dingbian_walkthrough.bat`。
可分发压缩包：`artifacts/release-0.25.0/FinishReview-v0.25.0-windows-x64.zip`。
复制到其他电脑时解压整个目录，保留 `_internal`，运行 `FinishReviewConsole.exe`。

新演练副本是 `D:\orca\count-lab\out\impl_20261014\ws_dingbian_v0250`。
只复制演练索引、芯片数据、校时和已有卡片；录像仍从原目录只读访问。
建立副本时没有复制旧的处理日志和逐人确认，初始汇总为：自动通过 233 人、
待处理 26 条、无法核实 3 人、已处理 8 条。用户开始操作后按实际处理日志恢复。
原 `ws_dingbian` 的操作记录保留。

更新最终 EXE 并重新打开演练后，仍为待处理 25 条、已处理 9 条；处理日志 SHA256
与更新前一致，已经完成的分析缓存保留。后台分析已停止，演练无需重新运行分析。

清单已生成，演练时无需重新分析。按
[定边演练步骤](FINISH_PROBLEM_LIST_WALKTHROUGH.md) 从打开清单到导出三张清单计时，
与原来的 98 分钟比较，并核对原 `终点复核清单.csv` 的补录 / 删除号码。

交接第 4 项要求“由用户实测”，方案第 11 节要求“每一步都单独修改、单独验收，
完成后再进入下一步”。本次软件检查不能代替这项操作验收。演练通过前不合并 main；
集团核对仍是独立原型，后续按第 5 项接入产品。

## 验证边界

本次不重跑三场真实录像的识别统计，原 26 / 61 / 55 条结果见
[原实施验收](FINISH_PROBLEM_LIST_VALIDATION.md)。也未把单独的集团核对原型当成产品功能，
未实现赛中实时分析或 CycleRace 接口扩展。

系统 Python 首次全量运行出现原生退出；使用项目虚拟环境执行回归。
一轮跨夜间暂停的回归中，双机位等待测试超过 620 ms，单独重跑通过；保留该轮日志
`pytest-final.log`，最终以恢复后的连续回归为准。
