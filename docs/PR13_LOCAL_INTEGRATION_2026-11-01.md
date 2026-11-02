# PR #13 本地整合与试用交接（2026-11-01）

## 授权及版本

- 用户要求打开包含 #13 的最新程序，并选择先整合当前工作树再试用。
- 本地分支：`fix/integrate-pr13-workbench`。
- 当前基础：`c2e97632282e5fdd7e1040a9117f32ff0b7da7e2`。
- PR #13 输入：`bcf4c6b16afc90099c59d39eaf754b71243429b8`；本轮查询时 PR 仍为 OPEN，head 未变化。
- 使用未提交的 merge 工作树运行；没有提交整合、推送、修改远端 PR、打包或替换现场程序。

## 整合范围

- 纳入 #13 的自动对应原帧绑定、时间配对保护、上下对应来源、自动接受与人工点数区分、跨段连续播放修复及相关测试。
- 保留当前小标签＋短引线＋空心圈；不恢复下方长竖标。
- 保留免原因本机排除、刷新失败保护、独立操作撤销、默认收起排除行、自动下一行、立即撤销。
- 保留精简顶部、更多菜单、缺终点线快捷入口和短圈数警告；长图例仅悬停显示。
- 删除合并后重复的旧排除方法，调整旧视觉测试与文案。源码涉及 auto_label、lap_wave、passage_review、quick_review_panel、race_strip、review_marker_style、review_window；详细文件列表以工作树 diff 为准。

## 本轮验证

- 13 个关联测试文件：**574 passed in 227.66s**。
  完整日志：`%TEMP%/finishreview-pr13-integration-related.log`。
- 最终规则及标记专项：**45 passed in 5.83s**。
  完整日志：`%TEMP%/finishreview-pr13-final-check.log`。
- Ruff、入口规则检查、暂存及未暂存 diff 空白检查通过；检查相关类未发现重复方法。
- 首轮专项为 171 通过、3 失败：旧长圈数文案、旧下方竖标期待、短状态缺“对应”两字；调整后取得上述通过结果。
- 真实赛事隔离副本：4 号打开绑定原帧 0；第 49 帧逐帧到下一段第 0、1 帧；连续播放能跨段继续；未写入人工判定。395 个源文件哈希未变。
  结果：`D:/tmp/finishreview-pr13-integrated-check-20261101b/result.json`。
  日志：`%TEMP%/finishreview-pr13-integrated-real-check-b.log`。
- 初次真实验收在 Qt 初始化前卡住，已停止；增加 Qt 插件路径配置后 Windows 平台重验退出 0。首次日志：`%TEMP%/finishreview-pr13-integrated-real-check.log`。未以卡住轮次冒称通过。

## 留给用户的窗口

- 运行生产 `FinishReviewWindow`，强制导入本工作树，不使用 editable install 的其他 checkout。
- 标题附带：`#13整合最新 · 隔离测试（两段录像）`。
- 独立目录：`%TEMP%/finishreview-pr13-manual-1c8yerbx/workspace`；复制验收副本并重写媒体路径，不操作原赛事目录。
- 只复制两段真实录像和相关索引/过线图，停在 4 号绑定原帧；其他行可能无可用录像，不代表完整赛事已验收。
- 未启动接收器或录像，不接管已有现场窗口；只安全点击复位，没有自动发送判定快捷键。窗口留给用户操作。
- 启动日志：`%TEMP%/finishreview-pr13-manual-launch.log`；截图：`%TEMP%/finishreview-pr13-manual-1c8yerbx/opened-window.png`。已实际查看截图，原帧与小标签可见。

## 限制及剩余事项

- 本轮没有运行完整 CI、打包验收或全赛事现场验收；关联专项不等于完整 CI。
- 自动对应不是号码识别或裁判确认；真实数据本轮未执行回车判定。
- 历史单进程全套 Qt 清理崩溃未在本轮扩大修复；此前分组结果不能替代本轮结果。
- 2026-11-02 复核发现运行窗口底部旧提示“看完本波回车记录”与 #13 新规则不一致，已改为“回车接受对应”；116 项专项通过，Ruff 和规则检查通过。
- 已重启最终隔离窗口，PID 33292；无接收器、无录像录制，窗口显示“回车接受对应”。
- 等用户试用反馈；提交整合、推送和部署仍需明确授权。保留既有未跟踪 output/，不提交生成数据。
