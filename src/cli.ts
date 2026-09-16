import { parseArgs } from 'node:util';
import { createInterface } from 'node:readline';
import { readFile, writeFile, mkdir, open, unlink, mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { configSchema, readConfig, seatNames, type Config } from './config.js';
import { Control, Stopped } from './control.js';
import { Journal } from './journal.js';
import { BrowserAdapter, launchBrowser, queryUrl } from './browser.js';
import { run } from './engine.js';
import { demoConfig, startDemo } from './demo.js';

const { values } = parseArgs({ options: {
  config: { type: 'string', default: 'config.json' }, mode: { type: 'string', default: 'dry-run' },
  setup: { type: 'boolean' }, demo: { type: 'boolean' }, now: { type: 'boolean' }, help: { type: 'boolean' },
} });
const control = new Control();
const rl = createInterface({ input: process.stdin, output: process.stdout });
let pending: ((answer: string) => void) | undefined;
const log = (message: string) => console.log(`[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${message}`);
const ask = (message: string): Promise<string> => {
  if (control.stopped) return Promise.reject(new Stopped());
  console.log(message);
  return new Promise(resolve => { pending = resolve; });
};
rl.on('line', line => {
  const answer = line.trim();
  if (pending) { const resolve = pending; pending = undefined; resolve(answer); return; }
  if (answer === 'p') { control.paused = true; log('已请求暂停；正在处理的网站响应仍可能完成。'); }
  else if (answer === 'r') { control.paused = false; log('继续执行。'); }
  else if (answer === 's') { control.stopped = true; log('已请求停止，浏览器将保留。'); }
});
rl.on('SIGINT', () => { control.stopped = true; if (pending) { const resolve = pending; pending = undefined; resolve(''); } });
rl.on('close', () => { control.stopped = true; if (pending) { const resolve = pending; pending = undefined; resolve(''); } });

async function setup(file: string): Promise<void> {
  console.log('配置保存在本机。不要输入密码或证件号码。成人与学生票可配置；儿童及其他特殊票种请人工购票。');
  const from = await ask('出发站（精确名称，例如 北京西）：');
  const to = await ask('到达站（例如 西安北）：');
  const travelDate = await ask('乘车日期 YYYY-MM-DD：');
  const saleDate = await ask('开售日期 YYYY-MM-DD（不是乘车日期）：');
  let time = await ask('开售时间 HH:mm（回车默认 09:15，北京时间）：');
  time ||= '09:15';
  const names = await ask('乘客姓名，多个用英文逗号分隔（需已添加到 12306）：');
  const passengers: Config['passengers'] = [];
  for (const name of names.split(/[,，]/).map(n => n.trim()).filter(Boolean)) {
    const ticket = await ask(`${name} 的票种：1 成人票 / 2 学生票（默认 1）：`);
    if (ticket && !['1', '2'].includes(ticket)) throw new Error('票种应为 1 或 2');
    passengers.push({ name, ticket: ticket === '2' ? '学生票' : '成人票' });
  }
  console.log('可用席别：' + seatNames.join('、'));
  const prefs = await ask('按优先级填写车次/席别，用逗号分隔，例如 G87/二等座,G89/一等座：');
  const preferences = prefs.split(/[,，]/).map(p => { const parts = p.trim().split('/'); if (parts.length !== 2) throw new Error('组合格式应为 车次/席别'); return { train: parts[0], seat: parts[1] }; });
  const berthAnswer = await ask('卧铺要求：1 不限 / 2 下铺优先 / 3 必须下铺（默认 1；2 和 3 会停在订单页交给你选铺）：');
  if (berthAnswer && !['1', '2', '3'].includes(berthAnswer)) throw new Error('铺位选项应为 1、2 或 3');
  const allowNoSeat = preferences.some(p => p.seat === '无座');
  const waitlistAnswer = await ask('无票时是否提醒你在网站提交候补？Y/n（默认 Y）：');
  const c = configSchema.parse({ from, to, travelDate, saleAt: `${saleDate}T${time}:00+08:00`, passengers, preferences, allowNoSeat,
    berth: berthAnswer === '2' ? '下铺优先' : berthAnswer === '3' ? '必须下铺' : '不限', waitlist: waitlistAnswer.toLowerCase() === 'n' ? '关闭' : '提醒' });
  await queryUrl(c); // validate exact station names before writing
  await writeFile(file, JSON.stringify(c, null, 2) + '\n', { mode: 0o600 });
  console.log(`已保存 ${path.resolve(file)}。建议先运行 npm run dry-run。`);
}
function alert(): void {
  process.stdout.write('\x07');
  if (process.platform === 'win32') {
    const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '[console]::Beep(880,400); [console]::Beep(1047,400)'], { windowsHide: true, stdio: 'ignore' });
    child.on('error', () => {}); child.unref();
  }
}
async function main(): Promise<void> {
  if (values.help) { console.log('npm run setup | npm run dry-run | npm start | npm run demo\n选项：--config 路径、--now（立即执行）、--mode live|dry-run。运行中输入 p/r/s 并回车暂停/继续/停止。'); return; }
  if (values.setup) { await setup(values.config!); return; }
  if (!['live', 'dry-run'].includes(values.mode!)) throw new Error('--mode 只能是 live 或 dry-run');
  const demo = values.demo ? await startDemo() : undefined;
  let context: Awaited<ReturnType<typeof launchBrowser>> | undefined;
  let lock: Awaited<ReturnType<typeof open>> | undefined;
  let tempProfile: string | undefined;
  const local = path.resolve('.local');
  const lockPath = path.join(local, 'run.lock');
  try {
    const config = demo ? demoConfig : await readConfig(values.config!);
    const target = demo?.url ?? await queryUrl(config);
    const guard = new Journal(path.join(local, 'attempts'), config);
    const dryRun = !demo && values.mode !== 'live';
    if (!demo && !dryRun) await guard.assertClear();
    await mkdir(local, { recursive: true });
    lock = await open(lockPath, 'wx');
    await lock.writeFile(String(process.pid));
    console.log(`\n${demo ? '本地模拟演练' : dryRun ? '只读演练（不点击预订）' : '正式购票（提交至待支付）'}\n${config.travelDate} ${config.from} → ${config.to}\n优先级：${config.preferences.map(p => `${p.train}/${p.seat}`).join(' → ')}\n乘客 ${config.passengers.length} 人；铺位：${config.berth}；无座：${config.allowNoSeat ? '接受' : '不接受'}\n开售：${config.saleAt}\n`);
    if (demo) tempProfile = await mkdtemp(path.join(os.tmpdir(), 'ticket-demo-'));
    context = await launchBrowser(config, tempProfile ?? path.join(local, 'browser-profile'));
    const page = context.pages()[0] ?? await context.newPage();
    const adapter = new BrowserAdapter(page, config, control, log, ask, !!demo);
    if (demo) {
      await context.route('**/*', route => new URL(route.request().url()).origin === demo.url ? route.continue() : route.abort());
      await page.goto(target);
    } else {
      await page.goto('https://kyfw.12306.cn/otn/resources/login.html');
      await ask('请在打开的浏览器手动登录，并检查是否已有未完成订单。准备好后回到此窗口按回车。');
      await control.checkpoint();
      await page.goto(target, { waitUntil: 'domcontentloaded' });
      await page.locator('input#fromStationText').waitFor();
      await adapter.verifyQuery();
    }
    log('输入 p/r/s 并回车：暂停 / 继续 / 停止。请保持电脑唤醒、联网。');
    const demoGuard = { assertClear: async () => {}, arm: async () => {} };
    const result = await run(config, adapter, demo ? demoGuard : guard, control, { dryRun, now: !!demo || values.now, log });
    if (result !== 'dry-run') alert();
    if (demo) log(`演练记录：提交 ${demo.records.submits} 次，点击支付 ${demo.records.payments} 次。`);
    if (result === 'handoff') process.exitCode = 2;
    // Explicitly release only the stop flag; this wait never restarts automation.
    control.stopped = false;
    await ask('自动操作已结束，浏览器继续保留。请先完成付款或人工处理；处理完后按回车关闭浏览器并退出。');
  } finally {
    await context?.close().catch(() => {});
    if (lock) { await lock.close(); await unlink(lockPath).catch(() => {}); }
    if (demo) await new Promise<void>(resolve => demo.server.close(() => resolve()));
    if (tempProfile) await rm(tempProfile, { recursive: true, force: true });
  }
}
main().catch(error => {
  if (error?.code === 'ENOENT') console.error('缺少配置或所需文件。首次使用请运行 npm run setup。');
  else if (error?.code === 'EEXIST') console.error('程序或本行程已有运行记录。请先确认没有其他运行实例和未完成订单，参阅 README 的恢复说明。');
  else console.error(error instanceof Error ? error.message : '启动失败');
  process.exitCode = 1;
}).finally(() => rl.close());
