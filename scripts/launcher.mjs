import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath } from 'node:url';

// Keep the Windows batch entry point ASCII-only. Node renders the Chinese menu.
const root = fileURLToPath(new URL('../', import.meta.url));
process.chdir(root);

const actions = {
  '1': { label: '配置行程', args: ['--setup'] },
  '2': { label: '本地模拟演练（不连接 12306）', args: ['--demo'] },
  '3': { label: '网站查询演练（手动登录，不下单）', args: ['--mode', 'dry-run'] },
  '4': { label: '正式运行（到待支付页停止）', args: ['--mode', 'live'] },
};

async function execute(args) {
  return new Promise(resolve => {
    const child = spawn(process.execPath, ['--import', 'tsx', 'src/cli.ts', ...args], {
      cwd: root, stdio: 'inherit', shell: false,
    });
    child.on('error', error => { console.error(`启动失败：${error.message}`); resolve(1); });
    child.on('exit', code => resolve(code ?? 1));
  });
}

async function main() {
  if (!existsSync('node_modules/tsx/package.json')) {
    console.error('依赖尚未安装，请在项目终端执行 npm install，再重新启动。');
    process.exitCode = 1;
    return;
  }
  for (;;) {
    console.log('\n12306 购票助手');
    for (const [key, action] of Object.entries(actions)) console.log(`${key}. ${action.label}`);
    console.log('5. 退出');
    const input = createInterface({ input: process.stdin, output: process.stdout });
    let selection;
    try { selection = (await input.question('请输入数字并回车：')).trim(); }
    catch { return; }
    finally { input.close(); }
    if (selection === '5') return;
    const action = actions[selection];
    if (!action) { console.log('请输入 1–5。'); continue; }
    if (['3', '4'].includes(selection) && !existsSync('config.json')) {
      console.log('\n还没有 config.json，先填写实际行程。配置完成后会返回菜单，请先运行网站查询演练。');
      await execute(['--setup']);
    } else {
      await execute(action.args);
    }
  }
}

await main();
