# FinishReview 架构路线图独立复核报告

> 复核日期：2026-08-24
> 仓库：`C:\Users\Administrator\Documents\trae_projects\video analysis\FinishReview`
> 分支：`main`
> 提交：`3ea2df18bc8cac1260417993136a8b3d735d353c`

## 一、总体结论

**结论：修订后执行。**

上一版路线图识别出了两个真实方向：主窗口职责过多，以及 CycleRace 接收链路缺少认证。但是，原路线图不能直接执行，主要原因如下：

1. CycleRace 未认证不是单纯的“接线遗漏”。`README.md:55`、`realtime/review_window.py:727-735` 和 `tests/realtime/test_review_window.py:672-708` 都明确把它定义为受信任局域网兼容模式。单边强制 Token 会直接破坏现有发送端兼容性。
2. “无认证警告”已经实现并有测试，原路线图重复建设。
3. 性能结论只根据循环复杂度推导，忽略了 `PassageReviewDialog._lookup_cache`、增量刷新和稳定片段绑定语义。索引化应先有规模基准，不能直接开工。
4. “结构化日志”“SQLite 查询投影”“TimingSource 接口”“EvidenceReviewService”等建议范围过大，部分属于当前规模下的过度设计。
5. 原路线图遗漏了两个有直接运行证据的问题：损坏配置可导致启动崩溃，以及本地打包脚本会使用 PATH 中的非项目 Python，污染发布目录。

本次没有修改生产代码。除本报告外，仓库源文件保持不变。

## 二、逐项复核表

| 原路线图项目 | 复核状态 | 代码证据 | 优先级是否合理 | 方案风险 | 修订建议 |
|---|---|---|---|---|---|
| 增加并保存 CycleRace `shared_token`，生产接收器必须接线 | 【部分证实】 | `realtime/passage_receiver.py:623-750` 的 `_handler_type()` 已实现 Bearer 校验；`PassageEventReceiver.__init__()` 在 `:761-796` 支持 `shared_token`；`realtime/review_window.py:2392-2423` 创建接收器时未传 Token。与此同时，`README.md:55` 和设置界面明确声明当前为无认证兼容模式。 | 原定“立即做、成本中”过于乐观。安全问题真实，但优先级取决于网络是否完全可信，且改动跨 FinishReview 与 CycleRace 发送端。 | 单边启用会使现有 CycleRace 全部请求返回 401；密钥分发、升级顺序和旧版本回退未设计。 | 改为跨系统协议迁移。先确认威胁模型和 CycleRace 发送端能力，再设计可灰度的 optional/required 两阶段启用。工作量中到大。 |
| 对无 Token 的 `0.0.0.0` 监听给出阻断或明确警告 | 【未证实】 | `realtime/review_window.py:727-735` 已显示“未启用认证，仅限受信任赛事局域网”；`tests/realtime/test_review_window.py:672-708` 明确断言该警告可见。 | 不应继续列为待办。 | 再增加重复警告会制造噪音；直接阻断会破坏兼容模式。 | 从路线图删除。仅在部署威胁模型改变后讨论阻断。 |
| 设置 HTTP 请求读取超时，500 只返回通用错误 | 【已证实】 | `_PassageHTTPServer` 在 `realtime/passage_receiver.py:523-525` 没有设置连接超时；`PassageRequestHandler.do_POST()` 在 `:659` 阻塞读取声明长度；`:705-707` 把 `str(error)` 返回客户端。 | P1 合理；在非可信网络中可升为 P0。 | 超时过短会误伤慢机器或拥塞 LAN；改错误响应可能影响依赖具体错误文本的发送端。 | 增加可配置的 5-10 秒读取超时，服务端记录完整异常，HTTP 500 返回固定错误码。保留 400/409 的协议校验文本。工作量小。 |
| 配置改为字段级解析，校验 `schema_version`，记录回退原因 | 【部分证实】 | `load_review_settings()` 在 `realtime/review_main.py:256-337` 用一个大 `try`；有效 JSON 根节点为列表时，`:258` 调用 `.get()` 会抛出未捕获的 `AttributeError`。`camera_index` 非整数会在 `:303` 触发总回退，导致 `high_speed_dir`、`timing_provider` 等后续字段未加载。测试仅覆盖正常往返、旧明文迁移和未知字段保留，见 `tests/realtime/test_review_main.py:215-478`。 | 字段级容错应提高到 P0/P1。强制拒绝未知 `schema_version` 则不合理。 | 严格版本拒绝会破坏现有旧版本迁移和未知字段保留行为。 | 先验证根节点是对象，再按字段独立解析并记录字段名。`schema_version` 用于迁移提示，不应无条件拒绝未来版本。增加非对象、坏单字段、坏密文组合测试。工作量小。 |
| 增加录像、配置、证据确认和预检结构化日志 | 【部分证实】 | `realtime/review_main.py:124-184` 已有轮转日志和全局异常钩子；`passage_receiver.py`、`racetiger_source.py`、`review_window.py` 已有关键异常日志。证据确认和删除本身已作为包含时间、revision、状态的追加记录写入 `passage_evidence_associations.jsonl`，见 `realtime/passage_evidence.py:229-279`；预检也有持久化日志，见 `realtime/preflight.py:191-235`。 | 全面结构化日志优先级被高估。少量生命周期日志有价值。 | 重复把正式证据写入运行日志会造成双重事实源，并增加敏感信息泄露面。 | 不引入日志框架。只补录像启动/停止、接收器启动失败、配置应用失败、RaceTiger 意外异常的普通参数化日志；证据事实继续以 JSONL 为准。工作量小。 |
| 增加 1000/5000 Passage 刷新性能基准 | 【已证实】 | 测试目录没有基准测试或 `pytest-benchmark`；`pyproject.toml:17-23` 也没有性能工具。`PassageReviewDialog.refresh()` 在 `realtime/passage_review.py:2137-2195` 会重建表格。 | P1 合理，且必须先于索引重构。 | 使用不真实的数据分布会得到误导结论；Qt 渲染和文件系统成本必须分别测量。 | 使用标准库 `time.perf_counter()` 即可，不必新增依赖。覆盖 500/2000/5000 事件、1/2 个机位、典型视频段数量和高速抓拍数量。工作量小。 |
| CI 增加 Ruff 和覆盖率报告 | 【部分证实】 | `.github/workflows/ci.yml:28-32` 只有 `compileall` 和 pytest；`pyproject.toml` 未配置 lint、类型检查或 coverage。现有 275 个测试覆盖所有 17 个源码模块，但没有覆盖率数据。 | 非阻断 Ruff 可列 P1；覆盖率门槛不应立即强制。 | 在 6000 多行 Qt 代码上直接启用严格规则会产生大量存量告警和无关改动。 | 先固定 Ruff 最小规则集并只检查新增/修改代码；先生成 coverage 报告观察基线，暂不设置硬门槛。工作量小到中。 |
| 抽出 `settings.py` 和 `ConfigService` | 【部分证实】 | `FinishReviewSettings` 定义在 `realtime/review_window.py:263-280`，入口为加载配置而从 UI 模块导入它，见 `realtime/review_main.py:28-31`。 | 移动 dataclass 合理；完整 `ConfigService` 优先级偏高。 | 一次迁移所有默认值、DPAPI、CLI override 和保存逻辑容易破坏兼容配置。 | 先新增轻量 `settings.py`，移动 dataclass 和纯解析函数；保留函数式 API，不引入状态型 Service。与配置容错修复一起做。工作量小到中。 |
| 抽出 `TimingSource` 接口，统一 CycleRace 与 RaceTiger 生命周期 | 【未证实】 | 两个来源都具有 start/stop 语义，但 CycleRace 是推送式 HTTP 服务，RaceTiger 是轮询客户端；`FinishReviewWindow.start_receiver()` 在 `realtime/review_window.py:2370-2492` 需要处理不同配置、状态和回调。当前只有两个实现，也没有第三来源需求。 | 当前不应列为近期必做。 | 为不相同的生命周期强行建立统一接口，可能把差异隐藏在大量条件分支中。 | 暂不抽象。若增加第三种计时源，或接收生命周期测试持续重复，再定义最小 `TimingController` Protocol。 |
| 抽出 `RecordingSessionController` | 【部分证实】 | `FinishReviewWindow` 在 `realtime/review_window.py:1570-3518` 同时持有 recorder、ring buffer、coordinator、publisher、timer 和 UI 状态；录像相关行为分散在 `_toggle_recording()`、`start_recording()`、`stop_recording()` 等方法。 | 方向正确，但不能作为一个短期大提交。 | 大范围移动 Qt 状态和线程清理逻辑容易造成录像未停止、worker 泄漏或设置回滚失效。 | 先补状态机特征测试，再按“创建 recorder”“启动/停止”“错误恢复”逐段抽取。每次保持现有工厂注入接口。总工作量大，拆成多个中等任务。 |
| 抽出 `EvidenceReviewService`，UI 不再直接写 Store | 【未证实】 | `PassageReviewDialog._confirm_pending_marker()` 和 `_delete_marker()` 在 `realtime/passage_review.py:2693-2713`、`:2844-2868` 直接调用 Store；但 `PassageEvidenceAssociationStore` 已完整封装校验、revision、原子追加和删除语义，见 `realtime/passage_evidence.py:116-307`。 | 当前优先级低。 | 新 Service 很可能只是转发 `confirm/clear`，增加层级但不增加业务约束。 | 暂不新增。只有证据确认规则需要被 GUI、CLI 或远程 API 共同复用时再抽取应用服务。 |
| 视频段按时间建立有序索引并二分定位 | 【部分证实】 | `VideoTimelineStore.locate_passage()` 在 `realtime/video_timeline.py:529-702` 是 O(S) 扫描；但 UI 使用 `_lookup_cache`，见 `realtime/passage_review.py:1888-1916`，并有稳定绑定测试 `tests/realtime/test_passage_review.py:1811-1857`。本次微基准中，5000 段时单次 lookup 约 1.42ms。 | 直接列短期实现不合理；先基准。 | 二分索引必须保留每 source 选择、边界容差、文件可播放优先级和“已绑定片段不自动跳转”语义，正确性风险高。 | 降为 P2。只有真实数据达到约定阈值，例如首次加载或刷新超过 500ms，才实现按 source/race/time 的索引。 |
| 建立 athlete、event row 和高速抓拍索引 | 【部分证实】 | athlete 线性匹配位于 `realtime/passage_review.py:2025-2037`；增量刷新线性找行位于 `:2227-2280`；高速抓拍 `min()` 扫描位于 `realtime/auyat_rgb.py:857-880`。但增量批次超过 64 条会回退全刷新，且查找结果有缓存。 | 原优先级偏高。 | 索引失效维护可能比当前扫描更复杂，尤其是表格插入/删除导致 row 变化。 | 先基准。athlete 字典是最低风险候选；event row 与高速二分仅在测得瓶颈后做。 |
| 移除宽泛 fallback import，统一包入口 | 【已证实】 | `passage_receiver.py:18-35`、`passage_review.py:50-93`、`review_window.py:44-145` 等捕获整个 `ImportError`。PyInstaller 本次分析因此报告 `video_timeline`、`passage_receiver` 等顶层模块缺失，虽然包级导入和 smoke test仍成功。正式入口已经是 `realtime.review_main:main`，见 `pyproject.toml:25-26`。 | P1 合理，但应在构建入口稳定后处理。 | 可能影响用户直接执行单个模块的非正式方式；一次改所有文件会扩大回归范围。 | 先确认只支持 `python -m realtime.review_main` 和 console script，再逐模块删除 fallback，并运行导入、pytest、PyInstaller 和 EXE smoke。工作量小到中。 |
| 保留 JSONL 审计源，增加 SQLite 查询投影 | 【未证实】 | Passage、timeline 和 evidence 都是每赛事目录内的 append-only 文件，并在启动时加载，见 `passage_receiver.py:324-487`、`video_timeline.py:190-340`、`passage_evidence.py:116-227`。没有真实文件规模、启动耗时或内存数据证明当前已超限。 | 当前不合理。 | 引入双存储一致性、投影重建、迁移和损坏恢复的新复杂度。 | 暂不处理。先收集最大赛事文件大小和启动时间。达到明确阈值后再评估轻量快照，SQLite 是后续选项而非默认答案。 |
| 为历史日志增加快照和压缩 | 【未证实】 | 全量读取存在，但日志按赛事目录隔离；没有压缩需求、磁盘增长或启动延迟数据。录像文件才是主要磁盘占用，JSONL 通常不是。 | 原路线图过早。 | 快照边界可能丢失 revision 历史，压缩会降低现场人工恢复便利性。 | 设触发条件：单个 JSONL 超过 100MB、启动回放超过 2 秒或真实内存压力。未触发前不做。 |
| 协议加入凭据轮换、发送端身份、心跳和兼容版本策略 | 【部分证实】 | 协议已有 `schema_version` 校验，见 `passage_receiver.py:191-245`；发现响应已有 `auth_required`，见 `:575-587`。没有心跳，UI 也明确提示无法判断发送端持续在线，见 `review_window.py:3250-3286`。 | 把四项绑成一个长期任务不合理。 | 会要求 CycleRace 同步升级，并引入密钥轮换失败、时钟和在线状态误判。 | 拆分：认证迁移按威胁模型；心跳按操作员需求；版本兼容继续沿用 schema；发送端身份只有多发送端或审计要求时再做。 |
| 建立真实 FFmpeg、RTSP、共享目录和 CycleRace 联调测试环境 | 【已证实】 | `.field_validation` 当前为空；测试大量使用 fake recorder、fake playback、临时 RGB 和进程替身。`tests/realtime/test_passage_video_integration.py` 是进程内 HTTP 集成，不是现场设备联调。 | 在大重构或正式发布前是 P1；不必把真实硬件塞入普通 CI。 | 自动化硬件环境维护成本高且易不稳定。 | 先建立可重复的人工验收清单和固定样本，再考虑专用测试机。工作量中；完整自动化为大。 |
| 增加数小时录像、网络中断、磁盘不足稳定性测试 | 【部分证实】 | 已有 recorder 失败、重启、journal 恢复等单元测试，但没有持续运行、真实磁盘或网络故障注入。 | 正式赛事发布前合理；日常重构前不是 P0。 | 测试耗时长、环境依赖高，可能产生大量录像数据。 | 建立 15-30 分钟加速故障注入测试；只有发布候选再执行数小时 soak test。 |

## 三、错误与遗漏

### 错误结论

1. **“无认证警告需要新增”错误。** 当前 UI 已明确警告，并由测试固定。
2. **“配置必须严格验证并拒绝 schema_version”不准确。** 当前测试明确要求旧版明文迁移和未知字段保留。真正缺陷是根节点类型未校验、字段解析不隔离。
3. **“没有缓存导致每次刷新 O(E × S)”不准确。** 首次 lookup 仍线性，但 `PassageReviewDialog._lookup_cache` 会保留已有定位，增量刷新也只刷新变化事件。
4. **“证据确认缺少审计日志”不准确。** `passage_evidence_associations.jsonl` 本身包含确认、删除、时间和 revision，是比运行日志更可靠的审计记录。

### 证据不足的结论

- 视频时间轴、高速抓拍和 athlete 扫描已经造成现场卡顿。
- JSONL 文件已经大到需要 SQLite、快照或压缩。
- 当前两个 timing provider 已经需要统一抽象接口。
- 现有测试覆盖率不足。没有 coverage 数据，只能说缺少覆盖率度量，不能直接说覆盖低。

### 严重程度被高估或低估的问题

**被高估：**

- 在明确受信任 LAN 的部署假设下，把 CycleRace Token 单边接线定为无条件 P0。
- 仅凭文件行数要求立即拆分两个 Qt 类。大文件是真实维护风险，但不等于当前正确性故障。
- 在没有规模数据前直接建设三类索引。
- 全面结构化日志、SQLite 投影和凭据轮换。

**被低估：**

- `load_review_settings()` 对 JSON 根节点非对象会启动崩溃；坏单字段会吞掉后续有效设置。
- `packaging/build.ps1:55` 使用 PATH 中的 `python`。本次系统 Python 构建把非项目依赖 `psutil` 和 `pyreadline3` 带入发布包，形成 221 个文件、475MB；将 `.venv\Scripts` 放到 PATH 后重新构建为 209 个文件、460MB，且不再包含这些包。
- `RaceTigerSource._run()` 在 `realtime/racetiger_source.py:492-517` 把所有异常都当 API 错误，仅 warning 不记录堆栈，程序缺陷可能永久重试而不易诊断。

### 路线图遗漏的问题

1. 配置文件根节点类型和字段级容错测试。
2. 构建解释器和依赖环境的可复现性；本地构建与 CI 构建的一致性。
3. 发布包依赖白名单或至少异常包扫描。
4. RaceTiger 预期网络异常与意外编程异常的分类记录。
5. PyInstaller clean build 仍报告 `Hidden import "sip" not found!`。当前 smoke test 通过，因此不是阻断问题，但应确认是无害警告并在构建文档中说明。

### 可能属于过度设计的建议

- 当前立即引入 `ConfigService` 对象；纯函数和 dataclass 已足够。
- 为两个语义不同的来源强制统一 `TimingSource`。
- 只为转发 Store 方法而增加 `EvidenceReviewService`。
- 在没有规模证据前引入 SQLite、快照和压缩。
- 一次性设计密钥轮换、发送端身份、心跳和协议升级框架。
- 在没有固定硬件实验室的情况下把真实设备测试放进普通 CI。

## 四、修订后的路线图

### P0：必须立即处理

#### 1. 修复配置加载的启动崩溃和连带回退

- **问题与影响：** 有效 JSON 如果不是对象，应用在创建 `QApplication` 前因 `AttributeError` 退出；一个字段损坏会使后续有效设置全部回退。
- **代码证据：** `realtime/review_main.py:256-337`，尤其是 `payload.get()` 和外层大 `try`。
- **推荐改法：** 验证根节点为 `dict`；每个字段用小型解析 helper 独立处理；记录字段名和回退值；保留旧明文迁移和未知字段。
- **涉及文件：** `realtime/review_main.py`、`tests/realtime/test_review_main.py`。可同步把 `FinishReviewSettings` 移至轻量 `realtime/settings.py`，但不是修复前置条件。
- **验证方式：** 新增根节点为 `[]`/`null`/字符串、坏 `camera_index` 加有效 RaceTiger 字段、坏路径、坏轮询间隔、不可解密密文组合测试；运行全量 pytest 和 packaged smoke。
- **工作量：** 小，约 0.5-1 人日。

#### 2. 固定发布构建使用的 Python 环境

- **问题与影响：** 本地正式构建可混入系统 Python 的无关包，导致包体增大、许可证和依赖边界漂移；CI clean build 无法发现本机发布污染。
- **代码证据：** `packaging/build.ps1:55` 直接调用 `python`；`README.md:21-25` 对开发命令使用 `.venv\Scripts\python`，但 `README.md:38-42` 的打包命令没有保证解释器。实际系统环境构建包含 `psutil`、`pyreadline3`，`.venv` 构建不包含。
- **推荐改法：** 给 `build.ps1` 增加 `-PythonPath`，默认优先仓库 `.venv\Scripts\python.exe`，否则明确打印并确认解释器；构建前检查核心依赖版本；发布检查增加禁止的非项目包列表或生成依赖清单。
- **涉及文件：** `packaging/build.ps1`、`packaging/assert_clean_distribution.ps1`、`tests/realtime/test_packaging_contract.py`、`README.md`。
- **验证方式：** 分别从污染的系统 Python 和干净 `.venv` 调用脚本，确认最终都使用指定解释器；比较包文件清单；运行 EXE `--smoke-test`。
- **工作量：** 小，约 0.5 人日。

### P1：近期处理

#### 1. HTTP 接收器边界加固

- **问题与影响：** 慢请求可长期占用线程；500/503 可能返回内部异常文本。
- **代码证据：** `realtime/passage_receiver.py:523-525`、`:635-707`。
- **推荐改法：** 在 server/handler setup 设置读取超时；只对协议错误返回可解释文本；内部错误返回固定 `internal_error`，详细异常仅写本地日志。
- **涉及文件：** `realtime/passage_receiver.py`、`tests/realtime/test_passage_receiver.py`。
- **验证方式：** 不完整 body 超时、内部 OSError 不泄露路径、正常/重复/冲突/401/503 行为不变。
- **工作量：** 小，约 0.5-1 人日。

#### 2. 构建和导入边界清理

- **问题与影响：** 大范围 `except ImportError` 会把模块内部真实依赖错误误判为顶层导入兼容问题，也产生 PyInstaller 缺失顶层模块警告。
- **代码证据：** `realtime/review_window.py:44-145`、`passage_review.py:50-93`、`review_recorder.py:19-49` 等。
- **推荐改法：** 先明确只支持包入口，再分批删除 fallback import；不要在同一提交重构业务逻辑。
- **涉及文件：** 上述使用 fallback 的 `realtime/*.py`、`tests/realtime/test_packaging_contract.py`、README。
- **验证方式：** 模块导入测试、全量 pytest、PyInstaller warning 对比、EXE smoke。
- **工作量：** 小到中，约 1-2 人日。

#### 3. RaceTiger 异常分类和有限生命周期日志

- **问题与影响：** 程序缺陷可能被永久当作网络错误重试；录像和接收器生命周期不易从运行日志还原。
- **代码证据：** `realtime/racetiger_source.py:492-517`；`review_recorder.py` 没有 logger；`review_window.py` 主要只记录异常。
- **推荐改法：** 对 `RaceTigerError` 等预期异常保留 warning；其他异常使用 `logger.exception` 并提供稳定状态码。只补 start/stop/failure 等生命周期日志，不复制证据 JSONL。
- **涉及文件：** `realtime/racetiger_source.py`、`review_recorder.py`、`review_window.py`、相关测试。
- **验证方式：** fake client 抛预期网络错误和意外 `AttributeError`，断言状态与日志级别；检查日志不含 Token/RTSP 密码。
- **工作量：** 小，约 0.5-1 人日。

#### 4. 建立规模基准，再决定性能改造

- **问题与影响：** 当前只有复杂度线索，没有真实 UI 延迟基线。
- **代码证据：** `passage_review.py:1888-1916` 已有缓存；`:2137-2299` 有全量和增量两条路径；测试未测耗时。
- **推荐改法：** 不新增第三方依赖，记录加载、全刷新、单事件刷新、时间轴 lookup 和高速 locate 的 p50/p95。
- **涉及文件：** 新增 `tests/performance/` 或不进入默认 pytest 的 `tools/benchmark_review.py`；不先改生产算法。
- **验证方式：** 记录固定硬件和数据规模；先制定阈值，例如增量刷新 <100ms、2000 条首次加载 <1s。
- **工作量：** 小，约 0.5-1 人日。

#### 5. 大重构前建立现场验收基线

- **问题与影响：** 单元测试无法验证真实 FFmpeg、USB/RTSP、共享目录、时钟和 CycleRace 联动。
- **代码证据：** `.field_validation` 为空；现有测试均为替身或临时文件。
- **推荐改法：** 先写人工验收清单和固定样本数据，记录版本、设备、网络、时区、录像时长和预期证据；不要求普通 CI 控制硬件。
- **涉及文件：** `.field_validation/` 或 `docs/field_validation.md`，以及固定非隐私样本的生成脚本。
- **验证方式：** 至少完成一次 CycleRace 推送、双机位录像、高速共享目录、重启恢复和证据确认闭环。
- **工作量：** 中，约 2-3 人日，加现场设备时间。

#### 6. 渐进拆分 `review_window.py`

- **问题与影响：** 单类同时管理设置、接收、录像、时间轴、worker 和状态 UI，后续改动容易扩大回归范围。
- **代码证据：** `FinishReviewWindow` 位于 `realtime/review_window.py:1570-3518`，54 个方法；构造函数直接创建全部运行组件，见 `:1640-1758`。
- **推荐改法：** 先移动 Settings dataclass/解析，再抽 receiver lifecycle，最后才抽 recorder lifecycle。每一步保持 UI 行为和注入点。
- **涉及文件：** `review_window.py`、`review_main.py`，新增小型 `settings.py`、可能的 `receiver_controller.py`；暂不拆 `passage_review.py` 的显示组件。
- **验证方式：** 现有 `test_review_window.py` 45 个测试，加启动/停止顺序、设置保存失败回滚、关闭窗口清理测试；每个拆分提交单独 build/smoke。
- **工作量：** 总体大，约 5-10 人日，必须拆成多次交付。

### P2：条件满足后再处理

#### CycleRace 认证迁移

- **触发条件：** 无法保证独立、隔离、受信任的赛事 LAN；或安全要求明确禁止匿名 HTTP。
- **方案：** FinishReview 和 CycleRace 同时支持 Token，先 optional 部署，再 required；DPAPI 保存密钥；发现协议继续发布 `auth_required`。
- **涉及文件：** FinishReview 的 `passage_receiver.py`、`review_main.py`、`review_window.py`、`secure_storage.py`，以及本仓库之外的 CycleRace sender。
- **测试：** 双版本兼容矩阵、密钥错误、发现协议、升级/回滚、重试队列。
- **工作量：** 中到大，约 3-5 人日，另加部署协调。

#### 时间轴、athlete 和高速抓拍索引

- **触发条件：** 基准或真实赛事证明 UI 延迟超过阈值；报告应包含 E/S/A/C 规模和耗时。
- **方案：** 先做 athlete 字典；视频按 race/source/time 建索引；高速抓拍使用有序起止时间二分。保留稳定片段绑定语义。
- **涉及文件：** `race_metadata.py`、`passage_review.py`、`video_timeline.py`、`auyat_rgb.py`。
- **测试：** 与当前线性算法做 property/差分测试，覆盖重叠片段、缺失文件、边界容差、午夜回绕和片段追加。
- **工作量：** 中，约 2-5 人日。

#### 录像控制器进一步抽取

- **触发条件：** P1 的现场基线和状态机测试完成，或新增第三机位、自动恢复策略等需求。
- **方案：** 把无 UI 的 recorder/ring-buffer/publisher 生命周期抽成 controller，Qt 只消费状态事件。
- **工作量：** 中到大，约 3-6 人日。

#### 快照、压缩或查询投影

- **触发条件：** 单赛事 JSONL >100MB、启动回放 >2 秒或内存出现可测压力。
- **方案：** 优先可校验快照；只有复杂查询需求真实出现时才评估 SQLite 可重建投影。
- **工作量：** 快照中；SQLite 大。

#### 稳定性和硬件自动化

- **触发条件：** 进入正式发布候选，且有固定测试机和可重复设备环境。
- **方案：** 先 15-30 分钟故障注入，再做数小时 soak；覆盖 RTSP 断开、共享目录离线、磁盘不足和应用重启。
- **工作量：** 中到大。

### 暂不处理

- 不立即引入 SQLite。
- 不建立完整 `ConfigService` 对象。
- 不为两个来源强制统一 `TimingSource`。
- 不建立仅转发 Store 的 `EvidenceReviewService`。
- 不建设新的结构化日志框架。
- 不强制拒绝未知配置 `schema_version`。
- 不设置硬覆盖率门槛。
- 不同时实施凭据轮换、设备身份、心跳和协议框架升级。
- 不仅因为文件行数就重写 `passage_review.py` 或 `video_playback.py`。

## 五、安全执行顺序

| 顺序 | 任务 | 前置依赖 | 主要回归风险 | 应增加或运行的测试 | 完成判定标准 | 回滚方案 |
|---:|---|---|---|---|---|---|
| 1 | 增加配置损坏特征测试 | 无 | 无生产行为变化 | 非对象 JSON、坏单字段、旧版密文、未知字段测试 | 新测试能稳定复现当前崩溃/连带回退 | 仅删除新增测试 |
| 2 | 修复配置加载 | 步骤 1 | 旧配置迁移、CLI override、DPAPI 行为变化 | `test_review_main.py`、全量 pytest、packaged smoke | 坏字段不阻止其他字段加载；旧版迁移和未知字段保留不变 | 回退解析 helper，保留步骤 1 作为缺陷证据 |
| 3 | 固定构建解释器 | 无，可与 1-2 并行设计但单独提交 | CI 路径、用户自定义 Python、FFmpeg staging | packaging contract、系统/venv 两种调用、dist 文件扫描、EXE smoke | 日志明确显示预期解释器；污染系统环境不能改变发布依赖 | 恢复旧脚本，继续只从干净 CI 构建 |
| 4 | HTTP 超时和错误脱敏 | 配置修复非必需 | CycleRace 重试行为、慢 LAN 请求 | receiver 全套测试、慢 body、内部路径脱敏、进程内集成 | 正常 ACK 不变；慢请求释放线程；500 不泄露内部文本 | 回退 timeout/响应格式；保留服务端异常日志 |
| 5 | RaceTiger 异常分类和有限日志 | 无 | 轮询线程意外停止或日志泄密 | fake client 异常分类、Token/密码脱敏、轮询恢复 | 预期错误继续重试；意外错误有堆栈且 UI 状态明确 | 恢复原 catch，保留测试作为观察项 |
| 6 | 删除 fallback import | 构建解释器已固定 | 直接运行单文件、PyInstaller hidden import | 全量导入、pytest、clean build、EXE smoke | 官方入口全部通过；缺失顶层项目模块警告消失 | 按模块逐个回退，不整体回滚 |
| 7 | 建立性能与现场基线 | 无代码结构前置 | 基准数据不真实 | 固定数据生成、重复测量、现场清单 | 有可复现规模、机器信息和阈值 | 无生产代码，删除基准即可 |
| 8 | 渐进拆分主窗口 | 步骤 2、5、7 完成 | Qt 生命周期、线程清理、录像恢复、设置回滚 | 每次运行相关 Qt 测试、全量 pytest、build/smoke、现场关键路径 | 每次只移动一个职责，行为和文件格式不变 | 每个抽取独立提交，逐提交回滚 |
| 9 | CycleRace 认证迁移 | 完成威胁模型、发送端方案和双版本测试 | 生产数据完全无法送达 | 双仓协议矩阵、升级/降级、错误密钥、重试队列 | optional 阶段稳定后再切 required；现场演练通过 | 配置回到 optional/无认证兼容模式，网络恢复受信任隔离 |
| 10 | 条件性能优化 | 基准超过阈值 | 片段选择和证据定位语义改变 | 新旧算法差分测试、真实样本回放 | 达到目标阈值且 lookup 结果逐项一致 | 保留线性实现作为 fallback，单独回滚索引层 |

## 六、最终建议

### 1. 最应该先做的三件事

1. 修复 `load_review_settings()` 的非对象 JSON 崩溃和坏字段连带回退，并补齐特征测试。
2. 固定 `packaging/build.ps1` 使用的 Python 解释器和依赖边界，防止本地发布包被全局环境污染。
3. 加固 HTTP receiver 的读取超时和内部错误脱敏；认证迁移先完成跨 CycleRace 的兼容设计，不要单边强制。

### 2. 哪些建议现在不要做

- SQLite 查询投影、JSONL 压缩、完整结构化日志平台。
- 强制统一 `TimingSource`、仅转发 Store 的 `EvidenceReviewService`。
- 没有基准就改造时间轴和高速抓拍索引。
- 没有发送端升级和部署方案就强制 Bearer Token。
- 一次性拆分全部 Qt 大文件。

### 3. 如果只能投入一天时间

完成两个小而确定的闭环：

1. 上午：给配置加载增加失败特征测试并修复根节点/字段级容错。
2. 下午：让构建脚本明确使用 `.venv` 或 `-PythonPath`，增加发布包异常依赖检查，执行 pytest、clean build 和 EXE smoke。

不建议在这一天启动 UI 大拆分、认证协议迁移或性能索引。

### 4. 开始重构前还需要补充的信息或测试

- 最大真实赛事的 Passage 数、视频段数、高速抓拍数、JSONL 大小和首次加载耗时。
- FinishReview 与 CycleRace 实际部署拓扑：是否专用交换机、是否有 Wi-Fi/访客设备、Windows 防火墙规则。
- CycleRace sender 当前是否支持 Authorization header、密钥配置和双版本灰度。
- 一套脱敏的真实赛事目录或可重复生成器。
- USB/RTSP 双机位、奥亚特共享目录和时钟偏移的现场验收记录。
- 录像连续运行、RTSP 中断、共享目录断开、磁盘不足和重启恢复的验收标准。

## 七、本次验证记录

### 成功执行

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q realtime tests
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest_tmp_independent_roadmap_review_venv
```

结果：

- Python 3.12.10
- NumPy 1.26.4
- OpenCV 4.11.0
- pytest 9.0.2
- PyInstaller 6.20.0
- `pip check`：`No broken requirements found.`
- pytest：两次独立全量运行分别为 `275 passed in 9.71s`、`275 passed in 10.58s`
- `compileall`：通过

只读缺陷复现与性能抽查：

- 将配置文件写成有效 JSON 数组 `[]` 后直接调用 `load_review_settings()`，稳定得到 `AttributeError: 'list' object has no attribute 'get'`。
- 将 `camera_index` 写成非法字符串，同时保留有效的 RaceTiger 字段，加载结果仍整体回退为 `timing_provider='cyclerace'`、空 `racetiger_base_url`。
- 5000 个 `RecordingSegment` 的 `VideoTimelineStore.locate_passage()` 白盒微基准为 median `1.293ms`、p95 `1.425ms`；该结果只用于证明当前不能仅凭线性扫描认定已形成瓶颈，不替代真实 UI/赛事基准。

构建验证：

```powershell
$env:Path="<repo>\.venv\Scripts;$env:Path"
.\packaging\build.ps1
artifacts\dist\FinishReviewConsole\FinishReviewConsole.exe --smoke-test
```

结果：

- PyInstaller clean build 成功。
- `assert_clean_distribution.ps1` 通过。
- 打包后 EXE smoke test：退出码 0。
- clean `.venv` 发布目录：209 个文件，460,372,213 bytes。
- PyInstaller 报告 `Hidden import "sip" not found!`，但当前 EXE smoke 通过；该警告记录为待确认项，不宣称已经解决。

### 构建环境对比证据

直接按当前 `build.ps1` 从系统 PATH 调用时：

- 使用系统 Python，而不是仓库 `.venv`。
- 发布目录为 221 个文件，474,901,805 bytes。
- 包含非项目依赖 `psutil` 和 `pyreadline3`。
- build 和 smoke 虽然通过，但依赖边界不再可复现。

### 未执行

- 未运行 Ruff、Mypy、Pyright 或 coverage，因为仓库依赖和 CI 中没有配置这些工具；没有安装新依赖。
- 未连接真实 CycleRace、RaceTiger、RTSP/USB 摄像机或奥亚特共享目录。
- 未执行数小时稳定性测试。

因此，本报告对源码结构、单元/进程内集成、打包和 EXE 启动具有较高可信度；对真实设备兼容、现场网络安全和长时间性能只能给出条件性结论。

## 八、修复执行记录（2026-08-24）

### 已完成

1. **配置容错：** `load_review_settings()` 现在校验 JSON 根节点，并按字段隔离 `source`、`camera_index`、目录和 RaceTiger 配置的解析失败。非对象 JSON 不再导致启动崩溃，坏 `camera_index` 不再阻止后续有效字段加载。
2. **配置分层首步：** `FinishReviewSettings` 已移动到纯数据模块 `realtime/settings.py`；`review_window.py` 继续重新导出该名称，保持现有导入兼容。
3. **构建解释器：** `packaging/build.ps1` 新增 `-PythonPath`，默认优先仓库 `.venv\Scripts\python.exe`，并明确打印实际解释器。
4. **发布依赖边界：** `packaging/assert_clean_distribution.ps1` 增加 `psutil`、`pyreadline3` 拦截，防止已确认的系统环境污染重新进入发布包。
5. **HTTP 边界：** `PassageEventReceiver` 新增默认 10 秒请求读取超时；不完整 body 返回 HTTP 408 和固定 `request_timeout`；503/500 分别返回固定 `delivery_failed`、`internal_error`，内部异常仅进入服务端日志。
6. **RaceTiger 诊断：** `RaceTigerError` 保持可预期 API 告警；其他异常记录完整堆栈，UI 只显示通用内部轮询错误。
7. **导入边界：** 9 个运行模块已移除宽泛 fallback import，统一使用 `realtime` 包相对导入。clean build 后不再出现项目顶层模块缺失告警。
8. **性能基线：** 新增 `tools/benchmark_review.py`，覆盖首次建表、全刷新、单事件刷新、时间轴定位和高速抓拍定位。
9. **现场基线：** 新增 `.field_validation/README.md`，定义 CycleRace、双机位、高速共享目录、重启、网络中断、磁盘不足和长时间运行的验收标准。

### 性能结果

固定在当前 Windows/Python 3.12.10 环境，使用 offscreen Qt 合成数据：

| 规模 | 首次建表 | 全刷新 p95 | 单事件刷新 p95 | 时间轴定位 p95 | 高速定位 p95 |
|---:|---:|---:|---:|---:|---:|
| 500 | 69.6 ms | 40.9 ms | 17.8 ms | 0.18 ms | 2.54 ms |
| 2000 | 171.3 ms | 97.7 ms | 33.7 ms | 0.72 ms | 7.54 ms |
| 5000 | 345.9 ms | 186.8 ms | 52.8 ms | 2.59 ms | 22.53 ms |

这些结果未触发本报告建议的索引优化阈值，因此未实施时间轴二分、event row 索引、高速抓拍索引或 SQLite 投影。真实赛事数据仍需按现场清单复测。

### 修复后验证

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q realtime tests tools
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest_tmp_roadmap_implementation_full
.\packaging\build.ps1
artifacts\dist\FinishReviewConsole\FinishReviewConsole.exe --smoke-test
```

结果：

- `pip check`：通过。
- `compileall`：通过。
- pytest：`283 passed in 10.07s`。
- PyInstaller clean build：通过，并明确使用仓库 `.venv`。
- 发布目录边界检查：通过，209 个文件，460,373,349 bytes。
- EXE smoke test：退出码 0。
- 项目顶层 fallback import 告警：已消失。
- `Hidden import "sip" not found!`：仍存在，但 smoke test 正常，继续作为非阻断待确认项。

### 仍按条件暂缓

- CycleRace 强制认证：等待发送端能力、威胁模型和双版本部署方案。
- 时间轴、高速抓拍和 event row 索引：等待真实赛事超过性能阈值。
- SQLite、快照和压缩：等待 JSONL 大小、启动耗时或内存压力达到触发条件。
- 录像控制器的自动恢复和更深层 UI 解耦：已完成创建及多机位启动/停止切片；自动重连、第三机位和状态事件仍等待现场基线后再设计。
- 真实硬件自动化和数小时 soak test：等待固定测试机、摄像机及共享目录环境。

## 九、录像会话控制器执行记录（2026-08-24）

### 本阶段范围

本阶段继续按“创建 recorder -> 启动/停止 -> 错误恢复”的顺序推进，只完成第二个切片。磁盘预检、Passage 注册、最终视频段发布、证据事实源和 UI 状态仍由 `FinishReviewWindow` 负责，没有迁移文件格式或正式证据语义。

### 已完成

1. **多机位会话控制器：** `realtime/recording_controller.py` 新增 `RecordingSessionController`，集中持有 `RecordingPipeline`，统一暴露 recorder、ring buffer、coordinator 和 publisher 映射。
2. **原子启动：** 单/双机位只有全部启动成功后才提交为活动会话；后续机位启动失败时逆序停止已启动机位，并保留原始异常。
3. **重复启动保护：** 相同来源的活动会话直接复用；不同来源不能覆盖仍在运行的会话。
4. **可重试停止：** 停止会尝试所有机位；已停止机位立即移出活动状态，仍在运行的失败机位继续由控制器持有，允许再次停止。
5. **窗口接线：** `realtime/review_window.py` 将多机位创建和停止委托给控制器，同时保留 `_recorders`、`_ring_buffers`、`_coordinators`、`_publishers` 兼容字段及原有最终片段发布顺序。
6. **设置安全边界：** 录像未完全停止时不再继续应用摄像头或目录设置；已保存的新设置会回滚，运行中的 recorder 仍可追踪。

### 新增测试

- `tests/realtime/test_recording_controller.py`：覆盖双机位组件映射、相同会话幂等启动、不同会话拒绝、第二机位失败回滚、停止幂等、部分停止失败重试。
- `tests/realtime/test_review_window.py`：覆盖窗口保留停止失败机位，以及设置应用在录像停止失败时回滚。

### 本阶段验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/realtime/test_recording_controller.py tests/realtime/test_review_window.py -q
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest_tmp_recording_session
.\.venv\Scripts\python.exe -m compileall -q realtime tests tools
.\.venv\Scripts\python.exe -m pip check
git diff --check
.\packaging\build.ps1
artifacts\dist\FinishReviewConsole\FinishReviewConsole.exe --smoke-test
```

结果：

- 相关测试：`60 passed`。
- 全量 pytest：`314 passed in 9.68s`。
- `compileall`、`pip check`、`git diff --check`：通过。
- PyInstaller clean build：通过，使用仓库 `.venv`。
- 发布目录边界检查：通过，209 个文件，460,384,133 bytes。
- EXE smoke test：退出码 0。
- `Hidden import "sip" not found!`：仍为已知非阻断告警。

### 尚未完成

- 未连接真实 USB/RTSP 双机位、CycleRace、RaceTiger 或奥亚特共享目录。
- 未执行网络中断、磁盘不足或数小时录像 soak test。
- 未实现自动重连、指数退避或控制器向 Qt 发射状态事件；这些属于下一段“错误恢复”，应在现场基线实际执行后再推进。

### Review 后修复（2026-08-24）

针对录像会话实现复核发现的失败路径问题，本阶段追加完成：

1. **保留真实 FFmpeg 句柄：** `FfmpegReviewRecorder.stop()` 只有在 `poll()` 确认进程退出后才清空 `_process` 和 stderr 线程；无法确认退出时保留句柄，允许后续重试。
2. **启动回滚可追踪：** 多机位启动或单管线组装失败时，未能停止的完整 pipeline 或不完整 recorder 会继续由 `RecordingSessionController` 持有，不再只记录日志后丢失引用。
3. **停止结果分级：** `RecordingStopFailure.still_running` 区分“仍在运行”与“已经停止但有退出告警”；只有前者阻止设置应用和窗口关闭。
4. **按机位封口归档：** 部分停止失败时，仍在写入的机位继续按 `recording=True` 处理，只有确认停止的机位发布最后一个归档段。
5. **错误状态恢复：** 停止重试成功后清除对应录像停止错误，不再让设备状态持续显示为异常。

新增测试覆盖 FFmpeg `wait/terminate` 失败、单管线清理失败、多机位启动回滚失败、已停止告警、部分停止归档状态和错误恢复。

修复后验证结果：

- 录像相关测试：`82 passed`。
- 全量 pytest：`320 passed in 9.06s`。
- `compileall`、`pip check`、`git diff --check`：通过。
- PyInstaller clean build及发布边界检查：通过，209 个文件，460,386,330 bytes。
- EXE smoke test：退出码 0。
- `Hidden import "sip" not found!`：仍为已知非阻断告警。

### 一键恢复录像（2026-08-24）

在缺少真实 USB/RTSP 现场基线的前提下，本阶段没有引入后台自动重连、指数退避或新的线程状态事件，而是先完成可由操作员明确触发、可通过现有测试验证的一键会话恢复：

1. **区分完整与残缺会话：** `FinishReviewWindow._recording_needs_recovery()` 在存在 recorder、但配置的全部机位未同时健康时判定需要恢复。
2. **明确操作状态：** 残缺会话的按钮显示为“恢复录像”，状态显示为“普通录像: 需要恢复”，并使用独立的警示色和提示文本。
3. **整组恢复：** 点击“恢复录像”会先停止控制器仍持有的 recorder，再按当前完整配置重建全部机位；健康会话仍保持原有“停止录像”行为。
4. **不改变证据语义：** 本次没有修改录像文件格式、视频段发布规则、Passage 关联或证据 JSONL。

新增 `tests/realtime/test_review_window.py::test_partial_camera_failure_uses_one_click_session_recovery`，覆盖双机位中一个 recorder 失效后显示恢复状态、一次点击替换两个 recorder、旧 recorder 全部停止且新会话恢复完整活动状态。

验证结果：

- 相关测试：`83 passed in 2.09s`。
- 全量 pytest：`321 passed in 9.53s`。
- `compileall`、`pip check`、`git diff --check`：通过。
- PyInstaller clean build及发布边界检查：通过，209 个文件，460,386,573 bytes。
- EXE smoke test：退出码 0。
- `Hidden import "sip" not found!`：仍为已知非阻断告警。

仍未覆盖真实 USB/RTSP 断线、网络抖动、磁盘不足和数小时录像；后台自动重连继续等待现场基线，不把单元测试中的 fake recorder 结果当作设备验收结论。
