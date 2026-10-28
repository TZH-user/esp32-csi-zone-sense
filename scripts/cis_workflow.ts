/* ESP32 CSI 多监测点扰动特征监控系统 · 实施工作流
 * 启动：CreateWorkflow(path=本文件, name="ESP32 CSI 监控系统实施", max_concurrency=2)
 * 执行依据：F:\ESP\projects\ESP32-CIS\docs\IMPLEMENTATION_PLAN.md
 */

interface StageResult {
  /** 本阶段是否通过验收判据。 */
  passed: boolean;
  /** 一句话结论。 */
  summary: string;
  /** 验收证据：跑过的命令与关键输出，或产出文件路径。 */
  evidence: string;
  /** 留给下游或人工的问题清单。 */
  openIssues: string[];
}

interface Verdict {
  /** 集成测试是否通过。 */
  passed: boolean;
  /** 不通过时给修复方的反馈，逐条引用关键报错。 */
  feedback: string;
}

interface Finding {
  /** 位置：文件路径或阶段标签。 */
  where: string;
  /** 问题或发现，一句话。 */
  what: string;
  /** 证据：命令与输出，或读到的内容。 */
  evidence: string;
  /** verified=独立复核或确定性检查确认；unconfirmed=未确认。 */
  status: "verified" | "unconfirmed";
  /** 严重程度，high 仅用于数据丢失/崩溃/结果错误。 */
  severity: "low" | "medium" | "high";
}

interface WorkflowReport {
  /** 两三句话回答：系统做到哪一步、验收情况。 */
  conclusion: string;
  findings: Finding[];
  /** 本 run 实际检查了什么、怎么查的。 */
  verified: string[];
  /** 没查或查不了的部分及原因。 */
  notCovered: string[];
}

const ENV = `环境事实：IDF v5.5.6，PowerShell 先执行 C:\\Espressif\\tools\\Microsoft.v5.5.6.PowerShell_profile.ps1 激活；IDF Python=C:\\Espressif\\tools\\python\\v5.5.6\\venv\\Scripts\\python.exe；官方仓库 F:\\ESP\\esp-csi；已构建参考副本 F:\\ESP\\projects\\csi_console_test（esp-radar 0.3.4 已缓存）；RX 工程 F:\\ESP\\projects\\ESP32-CIS\\ESP32_CIS_V1.0（esp32s3，串口 COM3）；TX1 经典 ESP32=COM8；路由器 TP-LINK_B7C4 信道 6。开工前先读 F:\\ESP\\projects\\ESP32-CIS\\docs\\IMPLEMENTATION_PLAN.md。`;
const HONESTY = `若检查无法通过、或需要人在现场操作（插板、走动测试、清场标定、提供路由器密码），用 escalate 如实提出，禁止伪造结果或绕过。`;

const findings: Finding[] = [];
const verified: string[] = [];

function collect(tag: string, r: StageResult) {
  if (r.passed) verified.push(`${tag}: ${r.evidence}`);
  for (const issue of r.openIssues) {
    findings.push({ where: tag, what: issue, evidence: r.evidence, status: "unconfirmed", severity: "medium" });
  }
}

artifact.board("progress", {
  title: "里程碑进度",
  key: "stage",
  status: "state",
  columns: ["passed", "failed"],
  cardTitle: "stage",
  detail: [{ field: "summary" }],
});

phase("备份原固件并烧录 TX 与 RX 最小闭环");
log("P1：并行备份两板 DAPLink 固件、烧 TX1 发送固件与 S3#1 最小接收工程");
const [tx1, rx1] = await Promise.all([
  agent("TX 烧录工程师", { system: `你负责 ESP32 发送节点固件。${ENV}${HONESTY}` }).ask<StageResult>(
    `任务：1) 用 esptool read_flash 备份当前两块在连板（COM3 的 S3 取 0x1000000 字节、COM8 的经典 ESP32 取 0x400000 字节）到 F:\\ESP\\projects\\ESP32-CIS\\scripts\\backup\\；2) 复制 F:\\ESP\\esp-csi\\examples\\get-started\\csi_send 到 F:\\ESP\\projects\\ESP32-CIS\\csi_tx_v1；3) 改配置：target=esp32、信道=6、SSID=TP-LINK_B7C4，密码若无法获知则 escalate 向用户索取；4) 构建并烧录 COM8，monitor 确认连上路由器且周期发包。按 IMPLEMENTATION_PLAN.md 第 4 节 TX 部分执行。返回 StageResult。`),
  agent("RX 工程工程师", { system: `你负责 ESP32-S3 接收节点工程。${ENV}${HONESTY}` }).ask<StageResult>(
    `任务：在 F:\\ESP\\projects\\ESP32-CIS\\ESP32_CIS_V1.0 实现 M1 最小 RX：从 F:\\ESP\\esp-csi\\examples\\esp-radar\\console_test 移植 sdkconfig.defaults（主 console 改 USB-Serial-JTAG）、partitions.csv、components/commands；新增 main/idf_component.yml（esp-radar>=0.3.4）；写最小 app_main.c：esp_radar_init 注册 wifi_radar_cb_ex 回调，回调内按源 MAC 打印幅值与 RSSI；set-target esp32s3 构建并烧录 COM3。判据：COM3 monitor 持续滚动含 TX1 MAC 的 CSI 幅值行。返回 StageResult。`),
]);
collect("P1-TX", tx1);
collect("P1-RX", rx1);
report({ stage: "P1-TX 烧录", state: tx1.passed ? "passed" : "failed", summary: tx1.summary }, "progress");
report({ stage: "P1-RX 最小工程", state: rx1.passed ? "passed" : "failed", summary: rx1.summary }, "progress");

phase("M1 验收：确认 CSI 数据流与幅值扰动");
const m1 = await agent("M1 数据流验收员", { system: `你只观察记录 CSI 数据流是否正常，不改固件。${ENV}${HONESTY}` }).ask<StageResult>(
  `以 2M 波特率开 COM3 monitor 记录 60 秒：1) 含 TX1 MAC 的 CSI 帧连续到达（间隔约 10ms）；2) 无人时幅值仅有环境小扰动；3) 如条件允许请在链路附近挥手确认幅值明显波动，需要人配合时 escalate；4) 样本日志存 F:\\ESP\\projects\\ESP32-CIS\\docs\\m1_sample.log。返回 StageResult。`);
collect("M1 验收", m1);
report({ stage: "M1 数据流验收", state: m1.passed ? "passed" : "failed", summary: m1.summary }, "progress");

phase("M2：检测状态机与上报协议（固件与 PC 并行）");
const [fw2, pc2] = await Promise.all([
  agent("M2 固件工程师", { system: `你在 RX 工程上实现检测与上报。${ENV}${HONESTY}` }).ask<StageResult>(
    `按 IMPLEMENTATION_PLAN.md 第 4、5 节实现：cis_protocol.h、cis_detect.c/h（per-peer 滑窗均值/方差/峰峰值 + 滞回去抖 FSM，自管上下文）、cis_report.c/h（串口 COM3@2M 二进制帧 0x01/0x10/0x11/0x30）、cis_console.c/h（usb_serial_jtag 主 REPL + TCP 2323；命令 peer/train/threshold/report/udp/dump）、cis_wifi.c/h、cis_peer.c/h、Kconfig.projbuild；构建烧录 COM3。判据：monitor 可见 0x10/0x11 帧的十六进制或调试打印随扰动变化。返回 StageResult。`),
  agent("M2 PC 端工程师", { system: `你写 PC 端解析与验证工具，纯标准库优先。${ENV}${HONESTY}` }).ask<StageResult>(
    `在 F:\\ESP\\projects\\ESP32-CIS\\pc_dashboard\\ 实现：protocol.py（帧 codec+crc16，纯标准库）、protocol_selftest.py（编解码往返/坏帧/失步重同步自测，纯标准库，退出码 0=全绿）、serial_source.py（COM3@2M 读线程+≥1MB 环缓冲+丢帧计数）、minimal_monitor.py（打印 EVENT 行）。用 IDF Python 跑通 selftest。返回 StageResult。`),
]);
collect("M2 固件", fw2);
collect("M2 PC", pc2);
report({ stage: "M2 固件检测+上报", state: fw2.passed ? "passed" : "failed", summary: fw2.summary }, "progress");
report({ stage: "M2 PC 解析", state: pc2.passed ? "passed" : "failed", summary: pc2.summary }, "progress");

phase("M2 集成验证与修复");
const selftest = await world.run("C:\\Espressif\\tools\\python\\v5.5.6\\venv\\Scripts\\python.exe", ["F:\\ESP\\projects\\ESP32-CIS\\pc_dashboard\\protocol_selftest.py"]);
if (selftest.exitCode !== 0) {
  log("协议自测未过，交给修复环处理");
}
let integrated = selftest.exitCode === 0;
let feedback = "首轮集成验证：按判据完整执行。";
const verifier = agent("集成验证员", { system: `你按判据做集成测试，只测不修。${ENV}${HONESTY}` });
for (let round = 1; round <= 3 && !integrated; round++) {
  const verdict = await verifier.ask<Verdict>(
    `运行 minimal_monitor.py --port COM3 --baud 2000000 验证：走动触发 EVENT MOTION、静置 60s 无误报、停走 3s 内 EVENT STILL、人为损坏字节流可重同步。需要人走动配合时 escalate。上轮反馈：${feedback}`);
  if (verdict.passed) {
    integrated = true;
    break;
  }
  feedback = verdict.feedback;
  await agent(`修复工程师-第${round}轮`, { system: `你按反馈修复固件或 PC 解析代码。${ENV}${HONESTY}` }).ask<StageResult>(
    `集成验证未过，反馈如下，请定位并修复（固件与 PC 两端都可改），修复后重新构建烧录或重跑自测：${feedback}。返回 StageResult。`);
}
verified.push(`M2 集成: protocol_selftest 退出码 ${selftest.exitCode}；集成验证轮数至多 3，最终 integrated=${integrated}`);
report({ stage: "M2 集成验证", state: integrated ? "passed" : "failed", summary: integrated ? "事件链路端到端通过" : "三轮修复后仍未过，见 findings" }, "progress");

phase("M3：桌面监控界面与交付文档（并行）");
const [ui3, doc3] = await Promise.all([
  agent("M3 界面工程师", { system: `你实现 Python 桌面监控界面。${ENV}${HONESTY}` }).ask<StageResult>(
    `按 IMPLEMENTATION_PLAN.md 第 6 节实现 ui_main.py/store.py/replay.py/udp_source.py/requirements.txt，并用 pip 安装依赖；另写 ui_smoke.py：启动主窗口 60 秒后 QTimer 自动退出、退出码 0。判据：界面含区域卡片、pyqtgraph 曲线、事件表、命令框、标定倒计时按钮；CSV 列齐全。返回 StageResult。`),
  agent("M3 文档工程师", { system: `你写交付文档与操作脚本，只写文档不改固件。${ENV}${HONESTY}` }).ask<StageResult>(
    `在 F:\\ESP\\projects\\ESP32-CIS\\ 产出：docs/标定SOP.md（第 7 节内容展开成可执行步骤）、docs/验收记录表.md（第 8 节 7 条用例的空白记录表）、scripts/flash_rx.bat、scripts/flash_tx.bat、scripts/backup_flash.sh。返回 StageResult。`),
]);
collect("M3 界面", ui3);
collect("M3 文档", doc3);
report({ stage: "M3 桌面界面", state: ui3.passed ? "passed" : "failed", summary: ui3.summary }, "progress");
report({ stage: "M3 文档脚本", state: doc3.passed ? "passed" : "failed", summary: doc3.summary }, "progress");

phase("M3 冒烟门");
const smoke = await world.run("C:\\Espressif\\tools\\python\\v5.5.6\\venv\\Scripts\\python.exe", ["F:\\ESP\\projects\\ESP32-CIS\\pc_dashboard\\ui_smoke.py"], { timeoutMs: 180000 });
let uiOk = smoke.exitCode === 0;
if (!uiOk) {
  await agent("界面修复工程师", { system: `你修复桌面界面冒烟失败。${ENV}${HONESTY}` }).ask<StageResult>(
    `ui_smoke.py 退出码 ${smoke.exitCode}，stderr 摘要：${smoke.stderr.slice(0, 2000)}。修复后保证 ui_smoke.py 退出码 0。返回 StageResult。`);
  const retry = await world.run("C:\\Espressif\\tools\\python\\v5.5.6\\venv\\Scripts\\python.exe", ["F:\\ESP\\projects\\ESP32-CIS\\pc_dashboard\\ui_smoke.py"], { timeoutMs: 180000 });
  uiOk = retry.exitCode === 0;
}
verified.push(`M3 冒烟: ui_smoke.py 最终退出码 ${uiOk ? 0 : "非0"}`);
report({ stage: "M3 冒烟门", state: uiOk ? "passed" : "failed", summary: uiOk ? "界面 60s 冒烟通过" : "冒烟仍失败，见 stderr" }, "progress");

phase("M4：多节点烧录与多 peer 联调");
const ports = await world.run("powershell", ["-NoProfile", "-Command", "[System.IO.Ports.SerialPort]::GetPortNames() -join ','"]);
log(`当前串口：${ports.stdout.trim()}`);
const [tx2, tx3] = await Promise.all([
  agent("M4 C3 节点工程师", { system: `你负责 C3 发送节点。${ENV}${HONESTY}` }).ask<StageResult>(
    `在串口列表中识别 C3 板（相对当前列表新出现的口；当前基线：${ports.stdout.trim()}）。若不在位则 escalate 请求用户插入 C3 板并告知 COM 号。到位后以 esp32c3 目标构建 csi_tx_v1 并烧录，monitor 确认连路由发包，记录其 MAC。返回 StageResult。`),
  agent("M4 经典2号节点工程师", { system: `你负责第二块经典 ESP32 发送节点。${ENV}${HONESTY}` }).ask<StageResult>(
    `在串口列表中识别经典#2 板（CH340 新口；当前基线：${ports.stdout.trim()}，注意排除 COM8 的 TX1）。若不在位则 escalate 请求用户插板并告知 COM 号。到位后以 esp32 目标构建 csi_tx_v1 并烧录，monitor 确认连路由发包，记录其 MAC。返回 StageResult。`),
]);
collect("M4-TX2", tx2);
collect("M4-TX3", tx3);
const m4 = await agent("多 peer 联调工程师", { system: `你做三链路多 peer 注册与标定联调。${ENV}${HONESTY}` }).ask<StageResult>(
  `在 RX 上 peer add 注册三条链路（TX1/TX2/TX3 的 MAC 与区域名），逐链路执行 train（需清场时 escalate 请求配合），验证：分别遮挡三条链路时 0x10 事件 MAC 正确无串扰；RX 断电重启后 peer 表与阈值从 NVS 恢复、30s 内恢复检测。返回 StageResult。`);
collect("M4 联调", m4);
report({ stage: "M4 多节点联调", state: m4.passed ? "passed" : "failed", summary: m4.summary }, "progress");

phase("汇总验收结果并产出交付报告");
const reportText = await agent("报告撰稿人", { system: `你把里程碑结果写成面向用户的中文交付报告，只依据给定事实，不补充未验证内容。` }).ask<string>(
  `依据以下事实写 markdown 交付报告（章节：结论、里程碑验收表、遗留与人工验收项、复现命令）：P1-TX=${JSON.stringify(tx1)}；P1-RX=${JSON.stringify(rx1)}；M1=${JSON.stringify(m1)}；M2 固件=${JSON.stringify(fw2)}；M2 PC=${JSON.stringify(pc2)}；M2 集成 integrated=${integrated}；M3 界面=${JSON.stringify(ui3)}；M3 冒烟 uiOk=${uiOk}；M4=${JSON.stringify(m4)}；findings=${JSON.stringify(findings)}。人工验收项（走动长测、30min 长稳、M5 七条用例）必须列为待人工执行。`);
await artifact.markdown("report", reportText, {
  title: "ESP32 CSI 监控系统交付报告",
  description: "M1~M4 各里程碑验收结果、遗留问题与人工验收清单。",
  primary: true,
});

const finalReport: WorkflowReport = {
  conclusion: `M1~M4 按判据推进：M1 数据流=${m1.passed ? "通过" : "未过"}，M2 集成=${integrated ? "通过" : "未过"}，M3 冒烟=${uiOk ? "通过" : "未过"}，M4 多节点=${m4.passed ? "通过" : "未过"}；M5 七条验收用例与长稳测试留给人工按 docs 执行。`,
  findings,
  verified,
  notCovered: [
    "M5 七条现场验收用例（需人在场地执行走动/静置/隔墙等动作）",
    "30 分钟 UI 长稳与误报率统计（人工项）",
    "答辩场地重标定排练（人工项）",
  ],
};
return finalReport;
