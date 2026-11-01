# 工作台控件精简交接（2026-11-01）

## 本轮授权及边界

用户同意精简日常工作台的缩放、高清、放大、定点回放入口，减少常驻说明；明确要求保留可直接点击的「1 非运动员、2 双芯片、3 补录、4 撤销」。正常历史复核不再展示「录像未启动、封存到、积压 0 秒」等占位信息。

仅修改界面呈现、交互接线和回归测试；不改正式计时、身份判定、导出规则、原始日志、赛事数据、录像及资源保护。用户在预览后授权本地提交；未授权本轮推送、合并、打包或部署，旧 PR #12 不在本分支处理。

## 修改范围

- `realtime/quick_review_panel.py`：四个行操作按钮复用原键盘处理；计数标题移至左表；短状态取代重复教学句；正常状态空行自动收起，终点线缺失保留提示。
- `realtime/passage_review.py`：新增工作台精简模式，隐藏手动缩放、比例、高清、定点回放及单机位放大入口；保留「复位画面」、滚轮缩放和中键平移；暂停自动读取同帧原图，失败可从短提示或复位按钮重试；普通模式保留原有控件。
- `realtime/review_window.py`：接入精简模式，多机位可显式放大；回到团首移入播放控制行；正常封存和分析水位转悬停，失败、暂停、缺证据及真实积压继续可见。
- 对应测试：`tests/realtime/test_passage_review.py`、`tests/realtime/test_quick_review_panel.py`、`tests/realtime/test_review_window.py`。

## 实际验证

- 按钮／笔记本布局／状态专项：9 passed。
- 首轮播放、到达表、主窗口集成：453 passed，2 failed。两处失败分别为旧单机位放大按钮行为断言、旧「未设置」文案断言；已按本轮业务需求更新，未取消终点线告警检查。
- 原图／精简控件专项（代理执行）：13 passed，175 deselected。
- 规则专项：21 passed。
- 全目录 Ruff、规则体积检查、205 个 Python 文件语法解析、`git diff --check`：通过。
- 正式入口 `python -B -m realtime.review_main --smoke-test`：退出码 0。
- 实际 Qt 窗口在独立临时空目录启动，1280×720，点击撤销并截图；另用独立生成的模拟录像解码到第 51 帧，点击复位画面并截图，四个按钮完整可见，回到团首处于播放控制行。未操作用户已有窗口，未使用真实赛事。模拟验证不等于真实赛事验收。
- 本机 Python 3.12 全量测试：1553 passed，426.22 秒，退出码 0（包含已更新的两个旧界面断言）。

## 日志与截图

- `%TEMP%/finishreview-compact-panel-tests.log`
- `%TEMP%/finishreview-compact-integration.log`（保留首轮失败详情）
- `%TEMP%/finishreview-compact-full-tests.log`
- `%TEMP%/finishreview-compact-checks.log`
- `%TEMP%/finishreview-compact-ui-la96sz3l/compact-1280x720.png`
- `%TEMP%/finishreview-compact-loaded-olwtgfzw/loaded-1280x720.png`（模拟录像）

## 截图反馈后的第二轮调整

用户再次授权处理截图中的占位和留白：状态、选中记录及行操作合并成单行；过线图号码带按可见标签实际需要保留一至三行；初始视口不再优先展示首芯片前的 30 秒人工边距，靠近右缘的播放游标会重新居中。完整时间范围与灰色证据缺口仍可浏览，未裁掉录像或改变判定。

新增影响文件：`realtime/race_strip.py`、`tests/realtime/test_race_strip.py`。第二轮首轮回归出现三处失败：新增初始视口测试没有考虑宽视口的右边界钳制；两处旧状态容器隐藏断言不适用于单行选中状态。已修正测试输入及单个状态标签可见性检查。第二轮相关回归重跑 300 passed，138.45 秒；后加的同排几何断言另跑 1 passed。完整日志：`%TEMP%/finishreview-compact-second-pass-rerun.log`。

实际模拟录像窗口已再次检查 1280×720，截图：`%TEMP%/finishreview-compact-loaded-s_4v4l27/loaded-1280x720.png`。Ruff 和差异空白检查通过。本轮不得沿用上一轮 1553 passed 声称第二轮全量通过。

## 剩余事项

真实赛事隔离验收、多机位现场可用性、Python 3.10 CI、打包及现场程序替换均未完成。本轮未删除或提交原有 `output/` 图片。
