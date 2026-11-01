# FinishReview 助手审核记录

本文件只供按需检索，不作为 Claude / Codex 自动加载的入口，不使用 `@` 导入本文件。
后续审核追加在这里，不向 `AGENTS.md` 或 `CLAUDE.md` 追加长篇历史。
原有方案、历史验收、交接与业务说明继续保留在原路径，不重复搬运或改写。

## 2026-10-27 建立轻量共享规则

- 授权：用户要求对 FinishReview 也做同类规则整理；范围为主线规则、独立审核记录、体积检查、专项测试和 CI 接入，不改业务源码或赛事数据。
- 初查：主线 `main` 基线 `668b188`，仅有既存未跟踪 `.claude/` 目录；该目录不修改、不暂存。主线及所列 Orca 工作区未发现项目级 AGENTS / CLAUDE 入口，没有长入口历史需要迁移。
- 规则依据：从现有 README 和系统说明提取项目定位及安全边界；不把 CycleRace 的 PyQt6、成绩计算规则或历史报告复制过来。新增共享 AGENTS 和只导入共享入口的短 CLAUDE，历史文档仍按需检索。
- 防复发：复用规则体积检查，限制单文件和合计体积，拒绝自动导入审核归档以及重新堆积审核摘要；接入现有 CI。此检查不是实时文件编辑拦截器，也不控制全局 Magpie 配置。
- 验证结果与收尾状态在完成后追加；尚未提交推送，不声称已在新会话或现有旧工作区自动生效。
- 完成验证：AGENTS 53 行 / 4578 字节，CLAUDE 3 行 / 47 字节；共享入口体积检查通过，专项 `21 passed`，新增 Python 文件 Ruff 与编译通过，`git diff --check` 通过。专项包含共享导入入口和 CI 检查顺序回归，其他用例覆盖单份/合计上限、历史导入、无效编码等。
- 当前 Codex 自审规则与 README/系统边界一致、短 CLAUDE 只导入共享入口、检查脚本兼容项目 Python 3.10 语法；改动仅为六个规则/工具/测试/CI 文件，既有历史文档、业务源码、赛事数据与未跟踪 `.claude/` 均未改动。未运行全仓业务测试、打包或现场录像验收，本次规则整理不宣称这些门禁通过。
- 本地整理完成，提交推送需用户明确授权；现有旧工作区不批量覆盖，未来新工作区需继承包含本轮改动的主线提交。未启动 Claude/Codex 新会话验证，不承诺消除所有压缩或给出实际费用降幅。
- 提交前复核：用户随后明确授权“同意提交并推送”。本轮 fetch 后本地 `main` 与 `origin/main` 均为 `668b188c84a02034688d122dbc81cd4c5ca31a61`，左右差异 0/0；重新运行专项 `21 passed`、新增 Python 文件 Ruff 和差异空白检查通过。只提交约定的六个文件，不暂存既有 `.claude/`，不合并其他分支，普通推送 `origin/main`；最终提交与推送结果以实际 Git 回执为准。

## 2026-11-01 PR #9 自动对上修复与本地验证

### 授权与业务范围

- 用户明确要求修复 PR #9 审查问题，并核对结果后决定提交、更新 PR 的后续步骤。本轮在 `自动对上` 工作区、基线 `e2d46902fd09093bf42da8e0ae2ef879a20fdb4a` 修复；不修改正式赛事数据，不安装、打包或替换现场程序。
- 用户此前同意将 PR #9 改为面向 main 并合并，但远端操作未成功；此前工具门禁要求的人类审查批准未解决，不能用自动审查代替或绕过。
- 修改源码：`realtime/arrival_review.py`、`auto_label.py`、`passage_receiver.py`、`quick_review_panel.py`、`race_metadata.py`、`workspace_context.py`。
- 测试：修改 `tests/realtime/test_auto_label.py`、`test_passage_review.py`、`test_quick_review_panel.py`，新增 `test_auto_label_regressions.py`；另在本文件记录实际验证。未修改规则入口或 CI、未跳过用例或修改 Qt 清理 fixture。

### 修复与自行复核

- 老格式元数据省略或以 null 表示未知圈数时，保留同赛事同组已知目标圈数；同 revision 不允许冲突的正数，新 revision 允许显式更新。接收回调交付实际存储快照并支持失败重试，工作区候选保存规范化后的快照，避免切回时丢失圈数。
- 自动接受先检查已点标记，统一保存本次新增标记；用 operation_id 精确追加撤回记录，不撤销去重返回的旧记录及其他操作。撤回失败时保留部分已存结果和对应标记，明确提示勿重复点人；成功撤回后清理本次临时创建的空候选。该流程不是原子事务，也不删除原始追加日志。
- 自动接受与状态栏使用同一观看范围（本分支为首尾各 700 ms）；普通人工波仍使用原范围。跨录像 session 按校时后过线时间排序，保留原始 recorder_ms、原帧位置；处理记录保存自动标号经操作员接受或人工点击的来源。
- 修复仅圈数、拒绝原因变化时的 UI 缓存刷新；坏/空 strip 跳过，原地修复后最多约一秒重新检查，避免每次 UI 刷新逐文件扫描。
- 当前执行助手逐项核对差异并补回归，未再启动大范围多代理审查。

### 实际测试与失败记录

- 受影响测试先完成 `325 passed`，日志 `D:/tmp/fr_latest_targeted_1101.log`；规则专项 `21 passed`。
- 首轮串行分批覆盖全部 94 个测试文件：9 批退出码均为 0，合计 `1495 passed`。日志目录 `D:/tmp/fr_all_batches_1101_final/`，汇总 `summary.json`，控制台汇总 `D:/tmp/fr_all_batches_1101_final_output.log`。该轮包含新增回归中的前 11 项。
- 随后补 5 项回归，第一次为 `15 passed, 1 failed`，日志 `D:/tmp/fr_latest_additional_1101.log`。失败用例在创建候选前改芯片 revision，创建过程正常刷新后已使用新记录，未实际模拟保存期间变化；调整为计划生成后、调用保存前改变记录。重跑新增文件为 `16 passed`，日志 `D:/tmp/fr_latest_additional_retry_1101.log`。
- 含全部新增回归的完整串行分批复验完成：全部 94 个测试文件、9 批退出码均为 0，合计 `1500 passed`（各批 165、92、97、352、236、164、184、35、175）。日志目录 `D:/tmp/fr_all_batches_1101_verified/`，逐批文件与退出码见 `summary.json`，控制台汇总 `D:/tmp/fr_all_batches_1101_verified_output.log`。未跳过测试；此前单进程 Qt 崩溃仍按下条保留，不将本结果冒称远端完整 CI 通过。
- 当前全范围 Ruff、规则体积检查、compileall、`git diff --check` 均退出 0，日志 `D:/tmp/fr_static_verified_1101.log`。
- 保留早先两轮单进程全套 native Qt 崩溃记录：`D:/tmp/fr_fix2_full_1101.log`（exit 139，清理时 access violation）及 `D:/tmp/fr_fix2_trace_1101.log`（exit 127，清理时 0xc0000374 heap corruption）。分批通过不证明其根因已解决，也不等于 Python 3.10/3.12 远端单进程 coverage CI 或打包通过。

### 真实副本与剩余门禁

- 对既有真实赛事隔离副本只读运行纯逻辑复核，已断言导入本工作区代码：36 条有效读取，27 条中间圈、9 条末圈，8 波；124 个检测过线均已校时，其中 76 个有遮挡。5 波符合自动配对条件，另外 3 波因人数不符、遮挡或小于 300 ms 拒绝。核心输入文件前后 SHA256 未变化。
- 结果 `D:/tmp/fr_realdata_latest_1101.json`；30 次缓存调用本次共约 0.0021 秒，只代表该副本、本机本次测量，不是全场性能保证。不将这些纯逻辑结果称为人眼号码核对、GUI 连续操作或真实联机验收。
- 只读查询 GitHub PR #9 时，代理 `127.0.0.1:10808` 连接被拒绝，无法刷新远端状态；没有擅自调整代理、账号或权限。本轮尚未提交、推送、修改 PR 目标、合并或关闭其他 PR。
- 后续仍需明确提交/推送范围、完成远端新头 CI 和工具执行审核，并保留真实赛事隔离 GUI/连续联机验收；在这些门禁完成前，不称为可替换现场程序的正式发布版。

### 两项阻塞后续处理（用户明确要求处理）

- GitHub 连接重新检查已恢复：原有代理 `127.0.0.1:10808` 现有监听，带代理的 gh 与 git 只读查询成功；未启动/重启代理或更改账号、系统代理及 Git 全局配置。
- 更正先前“所需人类批准”的来源：GitHub `branches/main/protection` 返回未保护，`rules/branches/main` 和 `rulesets` 均为空；仓库规则没有要求人类批准。此前阻塞来自工具执行审核，不据此绕过审核，也不冒称已有 Review。PR #9 当前 reviews 为空、仍为草稿，base 仍为多圈分支，head 仍为 e2d4690，既有 CI 仅验证旧头。
- 远端 main 已推进为 `b3f5bb2cc8fe9836c857c17f8628c1d57935ec52`（合并 PR #10 跨段边界修复），未自动合并或覆盖本轮未提交修复。后续若改变 PR 目标，须按此最新主线重新核对，不能沿用旧 base 的 CLEAN。
- 当前修复版单进程全套复现完成：`1500 passed in 357.48s`，退出 0，本轮未复现 Qt 崩溃；日志 `D:/tmp/fr_single_process_1101_current.log`、运行结果 `D:/tmp/fr_single_process_1101_current.json`。尚不能由一次通过证明历史偶发崩溃根因已消除。另委托单个只读分析任务检查 Qt 清理及对象生命周期，不并行运行 GUI 测试。
- 本机全局 pytest 9.1.1、ruff 0.16.9、coverage 未安装，与项目锁定 pytest 9.0.2、ruff 0.16.4、coverage 7.15.4 不同；Qt/OpenCV/NumPy 版本一致。不能将版本差异直接认作崩溃根因。
- 在临时 `D:/tmp/fr_ci_env_1101` 创建隔离 venv（共享已匹配锁定版本的运行依赖，仅在 venv 安装锁定测试工具），未改全局安装；隔离环境 pip check、锁定 Ruff、规则检查通过。安装命令的依赖安装退出 0，但随后控制台打印日志触发 GBK UnicodeEncodeError；已独立确认工具安装及版本正确。按 CI 单进程 coverage 方式复验，不修改 CI 或跳过测试。
- 锁定依赖 coverage 首轮完整运行未 native 崩溃，但 `1499 passed, 1 failed`（442.17 秒）；日志 `D:/tmp/fr_coverage_locked_1101/test.log`。唯一失败为录像锁测试将 Windows venv 启动器的 `Popen.pid`（6532）误认作实际持锁解释器 PID（30228）；程序确实拒绝了重复录像，不据此修改锁业务逻辑。
- 修改范围新增 `tests/realtime/test_recording_lock.py`：让持锁测试进程自报 PID，以实际 PID 核对 owner，并在 finally 终止实际持锁进程、等待启动器退出，保留强杀后重新获得锁的断言。全局环境与锁定环境各 `4 passed`，日志 `D:/tmp/fr_recording_lock_followup_1101/`；全范围锁定 Ruff 通过。
- 修正后的完整单进程 coverage 重跑中，日志 `D:/tmp/fr_coverage_locked_1101_retry/test.log`；结果到齐后补记，不以局部 4 项替代完整验证。
- 用户随后明确授权“提交并推送”：本次仅提交上述修复、录像锁测试修正及本审核记录，共 12 个文件，推送至原分支 `cocofee/自动对上` 更新 PR #9，不改 PR 目标、不合并、不发布或安装。提交前远端分支仍为 e2d4690、差异检查通过；完整 coverage 及只读 Qt 生命周期分析当时尚未返回最终结论。实际完成提交并推送 `b73072b8f8189484247576bcdaa83611490da5f9`。
- 锁定依赖 coverage 重跑完成：`1500 passed in 411.36s`，测试和 coverage report 均退出 0，总覆盖率 82%；日志 `D:/tmp/fr_coverage_locked_1101_retry/test.log`。只读 Qt 分析返回生命周期风险假设，但未确定旧崩溃根因；未按假设修改 cleanup 或产品线程逻辑。历史崩溃不再复现不等于根因彻底修复。

### 按用户授权合并到 main

- 用户要求“合并”，随后在执行审核要求的独立确认中明确选择“改为 main 并合并”；PR #9 目标已实际改为 main，未关闭其他 PR、未打包安装。
- b73072b 的远端两轮 Python 3.10/3.12 测试及 package 共 6 项全部通过；改目标后实际 mergeable 为 CONFLICTING，不能使用改目标前的 MERGEABLE 结论。
- 合入最新 origin/main `b3f5bb2cc8fe9836c857c17f8628c1d57935ec52`，唯一冲突文件 `realtime/passage_review.py`。保留主线录像失效保护、新录像真实帧率邻帧定位，以及分支的 archive 校时；邻帧边界以校时后的 target origin 计算，不混用 source 原起点。
- 新增 `test_boundary_seek_uses_aligned_archive_origin`，验证 120 ms 校时偏移在跨段时传到正确的邻帧边界；播放、校时和跨段相关测试 `291 passed in 82.52s`，日志 `D:/tmp/fr_merge_targeted_1101.log`。全范围锁定 Ruff、规则检查、compileall 通过。
- 最新主线整合后的单进程完整 coverage 仍在运行，日志 `D:/tmp/fr_merge_coverage_1101/test.log`；提交主线整合后需推送并等待该整合头的远端 CI，不把 b73072b 的成功沿用为整合头结果。
