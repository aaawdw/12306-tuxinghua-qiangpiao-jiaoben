import { createServer, type Server } from 'node:http';
import type { Config } from './config.js';

export const demoConfig: Config = {
  from: '北京西', to: '西安北', travelDate: '2026-09-30', saleAt: '2026-09-16T09:15:00+08:00',
  passengers: [{ name: '演练乘客', ticket: '成人票' }], preferences: [{ train: 'G87', seat: '二等座' }],
  allowNoSeat: false, berth: '不限', waitlist: '关闭', pollIntervalMs: 3000, maxRunMinutes: 0.1, browser: 'chrome',
};
const frame = (body: string, script = '') => `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本地流程演练 · 不连接 12306</title><style>body{font:18px system-ui;max-width:960px;margin:48px auto;padding:0 24px;color:#18283b}h1{font-size:28px}table{border-collapse:collapse;width:100%;margin:24px 0}td{padding:20px;border:1px solid #bbb}button,a,select,input{font:inherit;margin:8px;padding:8px}a{color:#1759b0;cursor:pointer}.up-box{border:2px solid #205ca8;padding:24px}aside{padding:16px;background:#edf5fd}</style><h1>本地购票流程演练</h1><aside>本页面的数据全部虚构，不会连接铁路网站或产生真实订单。</aside>${body}<script>${script}</script></html>`;
const query = frame(`<p>第一步：查询、匹配车次</p><input id="fromStationText" value="北京西"><input id="toStationText" value="西安北"><input id="train_date" value="2026-09-30"><a id="query_ticket">查询</a><table><tbody id="queryLeftTable"></tbody></table>`, `document.querySelector('#query_ticket').onclick=async()=>{await fetch('/otn/leftTicket/query');document.querySelector('#queryLeftTable').innerHTML='<tr id="ticket_demo"><td><a class="number">G87</a></td><td id="ZE_demo">有</td><td><a class="btn72" href="/order">预订</a></td></tr>';};`);
const order = frame(`<p id="ticketInfo_id">2026-09-30 G87次 北京西 → 西安北</p><ul id="normal_passenger_id"><li><input type="checkbox" id="passenger_demo"><label for="passenger_demo">演练乘客</label></li></ul><section id="passengers"></section><button id="submitOrder_id">提交订单</button><div class="up-box" style="display:none" id="confirmation">请核对：G87 二等座 演练乘客<button id="qr_submit_id">确认</button></div>`, `document.querySelector('#passenger_demo').onchange=e=>{document.querySelector('#passengers').innerHTML=e.target.checked?'<input id="passenger_name_1" value="演练乘客" readonly><select id="seatType_1"><option value="1">硬座</option><option value="O">二等座</option></select><select id="ticketType_1"><option value="1">成人票</option><option value="3">学生票</option></select>':''};document.querySelector('#submitOrder_id').onclick=()=>{document.querySelector('#confirmation').style.display='block';fetch('/record/submit',{method:'POST'})};document.querySelector('#qr_submit_id').onclick=()=>location.href='/otn/payOrder/init';`);
const payment = frame(`<h2>演练完成：待支付</h2><p>G87 · 北京西 → 西安北 · 二等座</p><p>演练乘客 · 金额：模拟数据</p><button id="pay">立即支付（演练按钮，不应被程序点击）</button>`, `document.querySelector('#pay').onclick=()=>fetch('/record/pay',{method:'POST'});`);
export async function startDemo(): Promise<{ url: string; server: Server; records: { submits: number; payments: number } }> {
  const records = { submits: 0, payments: 0 };
  const server = createServer((req, res) => {
    const pathname = new URL(req.url!, 'http://localhost').pathname;
    if (pathname === '/otn/leftTicket/query') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ status: true, data: { result: ['demo'] } })); return; }
    if (pathname === '/record/submit') { records.submits++; res.end('ok'); return; }
    if (pathname === '/record/pay') { records.payments++; res.end('ok'); return; }
    res.setHeader('Content-Type', 'text/html;charset=utf-8');
    res.end(pathname === '/order' ? order : pathname === '/otn/payOrder/init' ? payment : query);
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('演练服务启动失败');
  return { url: `http://127.0.0.1:${address.port}`, server, records };
}
