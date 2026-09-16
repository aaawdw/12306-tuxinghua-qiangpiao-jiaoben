import { chooseOffer, type Config, type Offer } from './config.js';
import { Control, Handoff, Stopped } from './control.js';
import type { SubmissionGuard } from './journal.js';

export interface Adapter {
  query(): Promise<Offer[]>;
  book(offer: Offer): Promise<void>;
  fillOrder(offer: Offer): Promise<void>;
  submit(): Promise<void>;
  waitForPayment(): Promise<void>;
  offerWaitlist(): Promise<void>;
}
export type RunResult = 'payment' | 'dry-run' | 'no-ticket' | 'handoff' | 'stopped';
export async function run(config: Config, adapter: Adapter, guard: SubmissionGuard, control: Control,
  options: { dryRun: boolean; now?: boolean; log: (message: string) => void }): Promise<RunResult> {
  let submitted = false;
  try {
    if (!options.dryRun) await guard.assertClear();
    const start = options.now || options.dryRun ? Date.now() : Date.parse(config.saleAt);
    if (!options.now && !options.dryRun && start < Date.now() - 60_000) throw new Handoff('开售时间已过去超过一分钟。请检查配置；确需现在执行时使用 --now。');
    if (start > Date.now()) options.log(`已就绪，等待 ${config.saleAt}。输入 p 暂停、r 继续、s 停止。`);
    while (Date.now() < start) await control.sleep(Math.min(1000, start - Date.now()));
    const end = Date.now() + config.maxRunMinutes * 60_000;
    let failures = 0;
    while (Date.now() < end) {
      await control.checkpoint();
      let offers: Offer[];
      try { offers = await adapter.query(); failures = 0; }
      catch (e) {
        if (e instanceof Handoff || e instanceof Stopped) throw e;
        if (++failures >= 3) throw new Handoff('连续三次查询异常，已停止自动查询，请检查浏览器。');
        options.log(`查询未完成，${failures * 10} 秒后重试。`);
        await control.sleep(Math.max(config.pollIntervalMs, failures * 10000));
        continue;
      }
      const offer = chooseOffer(config, offers);
      if (options.dryRun) {
        options.log(offer ? `演练匹配：${offer.train} / ${offer.seat}。未点击预订，未提交订单。` : '演练完成：当前没有符合条件的余票。未点击预订。');
        return 'dry-run';
      }
      if (offer) {
        options.log(`匹配 ${offer.train} / ${offer.seat}，正在核对订单。`);
        await control.checkpoint(); await adapter.book(offer);
        await control.checkpoint(); await adapter.fillOrder(offer);
        await control.checkpoint();
        await guard.arm(offer); // durable, exclusive guard before any order submission
        submitted = true;
        await adapter.submit();
        await adapter.waitForPayment();
        options.log('已到待支付页面。自动操作已停止，请核对订单并在网站提示期限内付款。');
        return 'payment';
      }
      options.log('本轮没有符合条件的余票。');
      await control.sleep(config.pollIntervalMs);
    }
    if (config.waitlist === '提醒') await adapter.offerWaitlist();
    options.log('已到查询截止时间，自动查询停止。');
    return 'no-ticket';
  } catch (e) {
    if (e instanceof Stopped) { options.log(e.message); return 'stopped'; }
    options.log(e instanceof Handoff ? e.message : '流程异常，已停止自动操作，请检查浏览器和配置。');
    if (submitted) options.log('本次可能已生成订单：请检查 12306 未完成订单；程序不会自动重提或取消订单。');
    return 'handoff';
  }
}
