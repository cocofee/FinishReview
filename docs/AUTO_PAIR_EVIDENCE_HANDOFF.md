# 自动对应与原帧统一：2026-11-01 收口记录

## 授权及边界

用户同意：补齐 PR #7 未进入发布包的末次观看余量调整；统一自动配对、画面与接受证据；取消自动对应的固定秒数补看门槛；复用现有时间配对器；验证跨段；圈数缺失明确提示。未授权本轮提交、推送、打包或更新桌面程序。

基底为主线 a64a950，工作分支 `fix/auto-pair-evidence-unified`。不修改原赛事、录像、校时模型、判读记录或 CycleRace；测试写入临时隔离目录。

## 行为

- 一波仍按相邻芯片间隔 <2 s 分组，已知目标圈数时只列末圈；缺目标圈数且已出现多圈记录时提示“目标圈数未收到，暂列全部圈次”，不猜、不回填。
- 自动对应沿用校时、人数、遮挡、过近、可能多人及已点附近等保护，另外复用 `crossing_pairing.pair_crossings` 检查每对时间与唯一性。默认 sigma=150 ms、gap_cost=1.6、window=700 ms；700 ms 只是候选搜索范围，不代表全部可接受，单对高代价约380 ms以上即可能被拒绝。不宣称能识别数量抵消下所有双芯片/漏读。
- 初次进入自动对应波，直接打开这波首位已配对人的原帧；不再独立找过线图变化后提前半秒。波内成员切换不跳帧，回到团首仍可显式打开绑定帧。原帧可按已有同画面对齐换到封存归档。
- 回车明确接受绑定证据，不要求固定前后秒数。不改历史日志格式，保存现有 `auto_label` 与 `auto_label_operator_accepted` 来源；界面显示“已接受对应”，不冒称人工点数或裁判确认，不将自动对应写入建议导出。
- 恢复 PR #7 末次调整：普通芯片波不留余量；无芯片行前后0.15 s；可能多人仍从更早到达处开始。人工处理的录像缺口及查看保护保留。
- 实际验收发现：跨短片播放时下一段元数据到达，FPS定位再次暂停，导致停在新段首帧。保留跨段导航架构，仅使延迟定位恢复已请求的播放状态；人工暂停仍暂停。

## 修改文件

产品：`realtime/auto_label.py`、`lap_wave.py`、`quick_review_panel.py`、`review_window.py`、`passage_review.py`。

回归：`tests/realtime/test_auto_label.py`、`test_lap_wave.py`、`test_quick_review_panel.py`、`test_auto_label_regressions.py`、`test_auto_pair_evidence.py`、`test_passage_review.py`。

文档：本记录及 `docs/LAP_FINAL_PLAN.md` 的现行入口提示；旧方案保留，不继续作为现行要求。

## 已实际验证

- 工作台/自动对应专项：125 passed（新增窗口归档用例前），`D:/tmp/fr_auto_pair_panel_third.log`。
- 含实际窗口解码的新增证据链回归：6 passed，`D:/tmp/fr_auto_pair_e2e_fixed.log`。首次窗口用例因测试缺绝对时间字段失败，修正后通过。
- 早一轮全量：1551 passed，`D:/tmp/fr_auto_pair_full.log`；不是最后跨段播放修复后的最终结果。
- 跨段边界专项：23 passed / 163 deselected，`D:/tmp/fr_auto_pair_seam.log`。
- Ruff、compileall、入口规则检查及 diff --check 已通过。
- 最终覆盖率首轮在 Qt fixture 清理 `tests/conftest.py:40` 出现原生崩溃，未完成：`D:/tmp/fr_auto_pair_final_full.log`。不能写为通过；未修改清理逻辑掩盖异常。
- 最终完整覆盖率重跑：**1554 passed，464.11秒，覆盖率82%**，`D:/tmp/fr_auto_pair_recheck_full.log`、`D:/tmp/fr_auto_pair_recheck_coverage.log`。包含跨段播放状态修复及窗口归档实际解码用例。
- 全量运行期间的末次来源显示调整（接受后仍显示灰蓝自动圈）及注释整理，再跑对应证据链、工作台、自动对应、圈数、规则专项：**147 passed，22.81秒**，`D:/tmp/fr_auto_pair_postreview.log`。Ruff与diff检查再次通过。

## 真实数据隔离验收

脚本：`D:/tmp/fr_auto_pair_validation_1101.py`，显式把本工作区放入 sys.path，避免 editable install 指错源码。只复制元数据、strips及所需两个 TS，UI所有录像引用重写为隔离目录。

成功结果：`D:/tmp/fr_auto_pair_real_1101f/result.json`；完整日志 `D:/tmp/fr_auto_pair_real_1101f.log`。

- 男子接力截图中 4号第1圈（此历史数据缺 target_laps，所以未隐藏）：新窗口实际打开绑定TS第0帧，画圈与对应号码来自同一绑定。
- 自动对应不提示“还差0.4秒”；标题明确显示缺目标圈数。不写真实赛事回车判定。
- 真实TS跨段：0019 第49帧 → 0020 第0帧 → 第1帧；连续播放也能跨入下一段继续。
- 首次逐帧成功但连续播放失败，触发上述延迟元数据暂停修复；修复后重新验收通过。
- 源数据与两段视频共395份文件哈希前后一致。
- offscreen截图在1366×768下验证结构位置，Qt文字渲染不完整，不能当作16英寸实际可见字体/布局验收。

## 剩余事项

本机全量覆盖率已通过；尚未执行 Python 3.10 / 远端 CI、打包或桌面安装验收。测试清理曾发生一次Qt堆损坏（0xc0000374），重跑通过不代表该环境生命周期问题已根治。未声称整场真实赛事人工验收通过。

集团检测漏人、遮挡下多人分离、异常优先工作台是后续工作；本轮不新增OCR或全程跟踪，不承诺大集团每人身份精准识别。正常对应仍是辅助证据，CycleRace继续为正式成绩唯一权威。

## 追加：上下同号竖线试做（2026-11-01）

用户明确授权写代码试做：去圆圈，上方过线图与下方视频用同号竖线，正常绑定无需逐个点选才显示；补颜色说明。仅显示变更，不新增配对算法、跟踪或判定写入；仍未授权提交、推送、打包、替换现场程序。

- 上下共用 `review_marker_style.py`：灰蓝虚线自动对应、绿实线人工处理、橙虚线待核；号码及图例提供非颜色提示。白字对比度分别4.62/5.85/6.27，通过4.5标准。
- 上方完整竖线放在绑定的视频过线时刻；未绑定芯片保留芯片时间参考并标“待核”。不改芯片时间。保存记录沿用 `_build_rows` 已解析的时间，避免重复校时。
- 下方工作台标记改成竖线及顶部号码。自动对应无需逐人选择，已保存绑定即使切换左表行也可在原绑定帧显示；重复来源去重。附近帧只保留淡线不带号码，不冒充全程跟踪。无可靠位置不硬贴号码。
- 播放紫线画在对应线下方，重合时保持对应状态颜色；终点线及旧辅助流程圆圈能力保留，不改存储格式或点击操作。
- 修改：`realtime/race_strip.py`、`quick_review_panel.py`、`passage_review.py`；新增 `review_marker_style.py`、`tests/realtime/test_correspondence_lines.py`；更新 `tests/realtime/test_review_window.py` 的圆圈断言。

验证记录：

- 初次专项错误：测试把芯片时间误当视频过线时间，另把strip时间数组写成list导致Qt绘制回调异常退出（127）。修正测试后6项通过，`D:/tmp/fr_correspondence_lines_retry3.log`。保留失败日志 `fr_correspondence_lines_tests.log`、`fr_correspondence_lines_retry.log`、`fr_correspondence_lines_retry2.log`。
- 首轮相关回归：491 passed / 1 failed，旧窗口用例仍要求圆圈；按本轮竖线要求更新断言。`D:/tmp/fr_correspondence_regressions.log`。
- 相关完整回归：**492 passed，204.98秒**，`D:/tmp/fr_correspondence_regressions_final.log`。末次保存时间复用及紫线覆盖修正后：**132 passed，21.29秒**，`D:/tmp/fr_correspondence_postreview.log`。Ruff、入口规则、diff检查通过。本轮显示修改后未重跑全项目覆盖率或远端CI。
- 真实男子接力隔离副本打开4号绑定TS帧0，逐帧及连续跨段通过，395份源文件哈希未变。`D:/tmp/fr_correspondence_real_1101a/result.json`。未对真实赛事按回车判定。
- 原生Windows窗口1366×768截图可读到上下4号、竖线及图例：`D:/tmp/fr_correspondence_visual_1101b/isolated_window.png`，结果及日志同目录/同名前缀。首次原生窗口执行在完成结果后退出139（`fr_correspondence_visual_1101a.log`），重跑退出0，不声称退出生命周期问题根治。
- 尚待用户以实际16英寸屏幕试用；密集团标签拥挤、无芯片匿名位置的上图展示、完整跟踪不是本次已完成能力。当前已打开的源码进程不会热更新；桌面旧包未替换。

### 用户试用后追加显示调整

用户现场截图反馈：视频贯穿长线遮挡，先缩短后又认为太短且绿色不明显；要求点击左表时上方对应线高亮，并收敛左表多种颜色。已授权只改显示：

- 视频工作台线最终为向绑定位置上方140屏幕像素、下方8像素，4像素状态色、白边及暗色外边，号码置于上端；不贯穿全屏。旧辅助流程不强制缩短。
- 上图选中记录使用加粗线、白色光晕及号码边框；按(芯片时间,号码)定位，不把所有同号圈次同时高亮，不改原跳帧逻辑。
- 左表自动对应灰蓝、人工处理绿、待核/疑点/补录/多余芯片橙、已排除灰；保留全部原状态文字。选中行原选择高亮不等于业务判定。
- 首轮113项回归有3个旧配色断言失败，按新配色调整后113 passed，23.80秒，`D:/tmp/fr_short_line_selection_final.log`。中等长度与白边末次回归：134 passed，20.28秒，`D:/tmp/fr_medium_line_final.log`；Ruff及diff检查通过。原生真实隔离截图 `D:/tmp/fr_medium_line_visual_1101a/isolated_window.png` 已看到加粗白边、中等长度线及上方选择边框；该隔离脚本完成结果后退出139，不能算正常退出通过。最新交互源码窗口已重开（PID27400），未启动录像或代做判定。未因配色改变正式记录。
- 曾在修改后未立即重开窗口，用户看到旧长线；正常关闭源码窗口再打开，不杀进程、不替换桌面包。

## 测试／非参赛芯片本机排除（2026-11-01）

用户报告女子接力19号为测试芯片，按1、删除、撤销都无法处理；明确同意新增可撤回的本机排除。只授权逻辑修复与隔离验证，未授权直接操作真实19号、提交、推送、PR或打包。

只读核实：界面提示“19号还没点人，先在画面上点中这个人，再按1”；旧按1仅操作已点出的人。撤销无效因本行未保存过判定。删除需“更多→删除选中的芯片记录”并填原因；未见删除标记，不宣称找到了删除故障根因。

实现：

- 最终用户要求免填写：未点人的有效芯片行按1或右键“非运动员（本机排除）”直接执行，不弹窗；固定保存该动作含义，沿用 `held` 及原因 `local_chip_exclusion`。不要求原帧，不伪造人物或确认。此前填写原因版本为试用历史，不是最终交互。
- 精确绑定赛事、阶段、事件、号码、芯片、时间、revision和active；保存前刷新，数据或工作区变化则拒绝。撤回只撤所选行。
- 显示灰色“已排除”，保留原因与“不导出/4撤回”说明；排除芯片不参与波内计数及自动配对，不进待处理、补录、建议删除导出。不修改芯片原始事件或CycleRace。
- 已点/已确认的人必须先撤销，原来“非运动员”的视频人物操作保留；同波其他已保存记录不动。4撤回，重开保留本机排除。
- `ArrivalReviewStore.save` 去重增加held原因比较，避免旧“看完无人”暂放记录吞掉本机排除；无新schema/action。

文件：`realtime/quick_review_panel.py`、`arrival_review.py`；`tests/realtime/test_local_chip_exclusion.py`（新增）、`test_quick_review_panel.py`、`test_arrival_review.py`；本交接。

验证：首轮94 passed（`D:/tmp/fr_local_exclusion_first.log`）；持久化与规则42 passed（`fr_local_exclusion_postreview.log`）；末次专项128 passed，24.17秒（`fr_local_exclusion_final.log`）。Ruff、compileall、入口规则、diff检查通过。补充同号不同圈、对话框期间切赛事、同波已保存身份不变、实际CSV导出无新增行，以及已排除行点击保护后：131 passed，19.59秒（`D:/tmp/fr_local_exclusion_guard_final.log`）。较大相关回归和全量结果待后台任务完成后登记。

真实女子接力19号隔离验收：`D:/tmp/fr_local_exclusion_real_1101a/result.json`，脚本 `D:/tmp/fr_local_exclusion_real_1101.py`。无帧排除→新面板重开仍排除→按4恢复待定，导出清单未变，1369份源文件哈希未变，退出0。原始19号未代操作。后续按用户要求已重开源码窗口，界面19号显示已排除，助手未执行真实判定；最后免填写调整尚需再次重开才生效。

## 本地提交收口

用户最新明确要求“这边直接提交”，授权提交当前分支代码；不含推送、创建PR、合并或打包。提交内容含自动配对证据统一、跨段播放修复、上下竖线/选择高亮/颜色收敛、测试芯片本机排除及免填写操作。

提交前末次专项：156 passed，28.08秒，`D:/tmp/fr_precommit_noinput.log`；Ruff、入口规则与diff检查通过。含免填写的键盘/右键直排、撤回、重开、跨赛事/圈次隔离、写入失败及导出保护。此前本机排除较大回归/全量后台日志未生成最终汇总（`fr_local_exclusion_regressions.log`、`fr_local_exclusion_full.log`），不能记为通过；前述1554项全量是更早版本的验证，不覆盖后续所有显示和排除修改。

用户最后反馈视频标记不够美观，要求轻描边、紧凑号码牌；随后中断并要求直接提交，因此该新一轮美化未实施，保留已验证的140像素4像素宽白边方案。不擅自把未完成美化写成已交付。
