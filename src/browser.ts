import { chromium, type BrowserContext, type Page, type Locator } from 'playwright';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { SEATS, normalizedText, type Config, type Offer, type Seat } from './config.js';
import { Control, Handoff } from './control.js';
import type { Adapter } from './engine.js';

const ORIGIN = 'https://kyfw.12306.cn';
export async function launchBrowser(c: Config, profile: string, headless = false): Promise<BrowserContext> {
  return chromium.launchPersistentContext(profile, {
    channel: c.browser === 'chromium' ? undefined : c.browser, headless,
    viewport: null, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    // Normal browser, with no stealth patches, proxy rotation, or CAPTCHA bypass.
  });
}
export async function queryUrl(c: Config): Promise<string> {
  const stations = JSON.parse(await readFile(fileURLToPath(new URL('../data/stations.json', import.meta.url)), 'utf8')) as Record<string, string>;
  if (!stations[c.from] || !stations[c.to]) throw new Error('找不到精确车站名称，请在配置中使用 12306 车站名称（例如“北京西”）。');
  const url = new URL('/otn/leftTicket/init', ORIGIN);
  url.search = new URLSearchParams({ linktypeid: 'dc', fs: `${c.from},${stations[c.from]}`, ts: `${c.to},${stations[c.to]}`, date: c.travelDate, flag: 'N,N,Y' }).toString();
  // 12306 splits fs/ts on literal commas before decoding station names.
  return url.href.replace(/%2C/gi, ',');
}
function escapeRegex(text: string): string { return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
async function visible(locator: Locator): Promise<boolean> { return locator.first().isVisible().catch(() => false); }

export class BrowserAdapter implements Adapter {
  constructor(readonly page: Page, readonly config: Config, readonly control: Control,
    readonly log: (text: string) => void, readonly ask: (text: string) => Promise<string>, readonly demo = false) {
    page.setDefaultTimeout(8000);
    page.setDefaultNavigationTimeout(30000);
    page.on('dialog', async dialog => {
      // Unknown confirmations are never accepted automatically.
      await dialog.dismiss().catch(() => {});
      this.dialogSeen = true;
    });
  }
  private dialogSeen = false;
  private currentOffer?: Offer;
  private async check(): Promise<void> {
    await this.control.checkpoint();
    if (this.page.isClosed()) throw new Handoff('浏览器已关闭。');
    if (!this.demo && new URL(this.page.url()).origin !== ORIGIN) throw new Handoff('页面已离开 12306 购票站点，请人工检查。');
    if (this.dialogSeen) throw new Handoff('网站弹出了未识别的确认框，已暂停自动操作，请人工检查。');
    if (await visible(this.page.locator('.nc-container:visible, .nc_wrapper:visible, #slide-passcode:visible, #J-login:visible'))) {
      throw new Handoff('网站要求登录或验证码核验，请在浏览器完成后检查当前订单状态。');
    }
  }
  async verifyQuery(): Promise<void> {
    await this.check();
    const actual = await this.page.locator('input#fromStationText').inputValue();
    const to = await this.page.locator('input#toStationText').inputValue();
    const date = await this.page.locator('input#train_date').inputValue();
    if (actual !== this.config.from || to !== this.config.to || date.slice(0, 10) !== this.config.travelDate) {
      throw new Handoff('查询页的车站或日期与配置不一致，已停止。请检查配置，不要在运行中手动修改查询条件。');
    }
    if (!this.demo && await visible(this.page.locator('#login_user')) && normalizedText(await this.page.locator('#login_user').innerText()) === '登录') {
      throw new Handoff('查询页面仍显示未登录，请先登录后重新启动。');
    }
    for (const id of ['autoSubmit', 'partSubmit', 'auto_query']) {
      const checkbox = this.page.locator(`input#${id}`);
      if (await checkbox.count() && await checkbox.isChecked()) {
        if (!await visible(checkbox)) throw new Handoff('网站自身的自动提交或自动查询选项已开启且不可见，请在网站关闭后重试。');
        await checkbox.uncheck();
      }
    }
  }
  async query(): Promise<Offer[]> {
    await this.verifyQuery();
    const query = this.page.locator('#query_ticket');
    await query.waitFor({ state: 'visible' });
    if (/btn-disabled/.test(await query.getAttribute('class') ?? '')) throw new Error('查询按钮尚不可用');
    const responsePromise = this.page.waitForResponse(r =>
      new URL(r.url()).pathname.match(/^\/otn\/leftTicket\/query[A-Za-z]*$/) !== null && r.request().method() === 'GET', { timeout: 20000 });
    const [response] = await Promise.all([responsePromise, query.click()]);
    if (response.status() === 429 || response.status() === 403) throw new Handoff('网站限制了当前请求，已停止自动查询，请按网站提示处理。');
    if (!response.ok()) throw new Error('查询响应异常');
    const data = await response.json().catch(() => null);
    if (!data || data.status !== true || !Array.isArray(data.data?.result)) {
      throw new Handoff('网站未返回有效的查询结果，可能需要验证或页面已变化，请检查浏览器。');
    }
    await this.page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await this.check();
    const rows = await this.page.locator('#queryLeftTable tr[id^="ticket_"]').evaluateAll(elements => elements.filter(el => (el as HTMLElement).offsetParent !== null).map(el => ({
      rowId: el.id,
      train: el.querySelector('a.number')?.textContent?.trim() ?? '',
      bookable: Array.from(el.querySelectorAll('a.btn72')).some(a => a.textContent?.trim() === '预订' && !a.classList.contains('btn-disabled')),
      cells: Array.from(el.querySelectorAll('td[id]')).map(td => ({ id: td.id, text: td.textContent?.trim() ?? '' })),
    })));
    const offers: Offer[] = [];
    for (const pref of this.config.preferences) {
      for (const row of rows.filter(r => r.train === pref.train)) {
        const cell = row.cells.find(cell => cell.id.startsWith(SEATS[pref.seat]));
        if (cell) offers.push({ ...pref, rowId: row.rowId, availability: cell.text, bookable: row.bookable });
      }
    }
    return offers;
  }
  async book(offer: Offer): Promise<void> {
    await this.check();
    const row = this.page.locator('#queryLeftTable tr').filter({ has: this.page.locator('a.number').filter({ hasText: new RegExp(`^${escapeRegex(offer.train)}$`) }) });
    if (await row.count() !== 1 || await row.getAttribute('id') !== offer.rowId) throw new Handoff('目标车次发生变化，请人工检查。');
    await row.getByRole('link', { name: '预订', exact: true }).click();
    try { await this.page.locator('#normal_passenger_id').waitFor({ state: 'visible', timeout: 20000 }); }
    catch { throw new Handoff('未能进入乘客确认页，可能需要核验或余票变化，请检查浏览器。'); }
  }
  private async verifyTrip(offer: Offer): Promise<void> {
    const info = this.page.locator('#ticketInfo_id');
    if (!await visible(info)) throw new Handoff('无法识别订单行程区域，停止提交，请人工核对。');
    const rawText = await info.innerText();
    const text = normalizedText(rawText);
    const [y, m, d] = this.config.travelDate.split('-');
    const dates = [this.config.travelDate, `${y}年${m}月${d}日`, `${y}年${+m}月${+d}日`];
    const stationPattern = (station: string) => new RegExp(`(?:^|[^\\p{Script=Han}]|次)${escapeRegex(station)}(?:站)?(?![\\p{Script=Han}])`, 'u');
    const from = stationPattern(this.config.from).exec(rawText);
    const to = stationPattern(this.config.to).exec(rawText);
    if (!new RegExp(`(^|[^A-Z0-9])${escapeRegex(offer.train)}([^0-9]|$)`).test(rawText)
      || !from || !to || from.index >= to.index || !dates.some(date => text.includes(date))) {
      throw new Handoff('订单行程与配置不符或无法完整识别，已停止提交。');
    }
  }
  async fillOrder(offer: Offer): Promise<void> {
    await this.check(); await this.verifyTrip(offer);
    const selected = this.page.locator('#normal_passenger_id input[type="checkbox"]:checked');
    while (await selected.count()) { await this.check(); await selected.first().uncheck(); }
    for (const passenger of this.config.passengers) {
      await this.check();
      const labels = this.page.locator('#normal_passenger_id label');
      const matches: Locator[] = [];
      for (let i = 0; i < await labels.count(); i++) {
        const text = normalizedText(await labels.nth(i).innerText());
        if (text === passenger.name || text === `${passenger.name}(学生)` || text === `${passenger.name}（学生）`) matches.push(labels.nth(i));
      }
      if (matches.length !== 1) throw new Handoff('无法唯一匹配配置中的乘客，请在浏览器核对乘客名单。');
      const id = await matches[0].getAttribute('for');
      if (!id) throw new Handoff('乘客选择控件已变化，请人工处理。');
      await this.page.locator(`input[id=${JSON.stringify(id)}]`).check();
    }
    const seats = this.page.locator('select[id^="seatType_"]');
    const tickets = this.page.locator('select[id^="ticketType_"]');
    if (await seats.count() !== this.config.passengers.length || await tickets.count() !== this.config.passengers.length) throw new Handoff('订单乘客数量与配置不一致，请人工核对。');
    for (let i = 0; i < this.config.passengers.length; i++) {
      await this.check();
      // Use the actual order row name, because the website may insert rows in a different order.
      const suffix = (await seats.nth(i).getAttribute('id'))!.slice('seatType_'.length);
      const nameInput = this.page.locator(`input[id="passenger_name_${suffix}"]`);
      if (await nameInput.count() !== 1) throw new Handoff('无法验证订单行中的乘客姓名，请人工核对。');
      const name = (await nameInput.inputValue()).trim();
      const person = this.config.passengers.find(p => p.name === name);
      if (!person) throw new Handoff('订单中出现未配置的乘客，已停止。');
      await this.selectNamed(seats.nth(i), offer.seat);
      await this.selectNamed(this.page.locator(`select[id="ticketType_${suffix}"]`), person.ticket);
    }
    if (this.config.berth !== '不限' && /卧/.test(offer.seat)) {
      throw new Handoff(`你设置了“${this.config.berth}”。当前版本请在此页人工选铺并提交；程序不会把铺位偏好当作已满足。`);
    }
    await this.verifyTrip(offer);
    this.currentOffer = offer;
    await this.verifyPassengers(offer);
  }
  private async verifyPassengers(offer: Offer): Promise<void> {
    const names = this.page.locator('input[id^="passenger_name_"]');
    const seats = this.page.locator('select[id^="seatType_"]');
    if (await names.count() !== this.config.passengers.length || await seats.count() !== this.config.passengers.length) throw new Handoff('订单人数已变化，已停止提交。');
    const seen = new Set<string>();
    for (let i = 0; i < await names.count(); i++) {
      const name = (await names.nth(i).inputValue()).trim();
      const passenger = this.config.passengers.find(p => p.name === name);
      if (!passenger || seen.has(name)) throw new Handoff('订单乘客与配置不一致，已停止提交。');
      seen.add(name);
      const suffix = (await names.nth(i).getAttribute('id'))!.slice('passenger_name_'.length);
      const seat = await this.page.locator(`select[id="seatType_${suffix}"] option:checked`).innerText();
      const ticket = await this.page.locator(`select[id="ticketType_${suffix}"] option:checked`).innerText();
      if (normalizedText(seat).split(/[（(]/)[0] !== offer.seat || normalizedText(ticket).split(/[（(]/)[0] !== passenger.ticket) throw new Handoff('已选择的席别或票种发生变化，已停止提交。');
    }
  }
  private async selectNamed(select: Locator, wanted: string): Promise<void> {
    const options = await select.locator('option').evaluateAll(opts => opts.map(el => ({ value: (el as HTMLOptionElement).value, text: el.textContent ?? '', disabled: (el as HTMLOptionElement).disabled })));
    const matching = options.filter(o => !o.disabled && normalizedText(o.text).split(/[（(]/)[0] === wanted);
    if (matching.length !== 1) throw new Handoff(`订单没有唯一可用的“${wanted}”选项，请人工核对。`);
    await select.selectOption(matching[0].value);
  }
  async submit(): Promise<void> {
    await this.check();
    if (!this.currentOffer) throw new Handoff('尚未完成订单核对，无法提交。');
    await this.verifyTrip(this.currentOffer);
    await this.verifyPassengers(this.currentOffer);
    await this.page.locator('#submitOrder_id').click();
    try { await this.page.locator('#qr_submit_id').waitFor({ state: 'visible', timeout: 20000 }); }
    catch { throw new Handoff('提交后未能识别确认窗口，请检查页面与未完成订单，程序不会重提。'); }
    await this.check();
    // Inspect only the active confirmation window, not hidden help text in the full page.
    const confirmationText = await this.page.locator('#qr_submit_id').evaluate(el => (el.closest('.up-box, .dhtmlx_window_active, .dhtmlx_window_inactive') as HTMLElement | null)?.innerText);
    if (!confirmationText) throw new Handoff('无法识别订单确认窗口，请人工确认。');
    const text = normalizedText(confirmationText);
    if (!this.config.allowNoSeat && /无座/.test(text)) throw new Handoff('网站确认窗口包含无座提示，已停止，请人工处理。');
    if (/部分提交|部分乘客|部分无票/.test(text)) throw new Handoff('网站提示部分乘客或部分提交，请人工核对。');
    await this.control.checkpoint();
    await this.page.locator('#qr_submit_id').click();
  }
  async waitForPayment(): Promise<void> {
    const end = Date.now() + 120000;
    while (Date.now() < end) {
      await this.control.checkpoint();
      if (this.page.isClosed()) throw new Handoff('浏览器已关闭，请检查 12306 未完成订单。');
      const url = new URL(this.page.url());
      if ((this.demo || url.origin === ORIGIN) && /^\/otn\/payOrder\/init/.test(url.pathname)) {
        const text = await this.page.locator('body').innerText();
        if (/席位已锁定|支付剩余时间|网上支付|立即支付|待支付/.test(text)) {
          await this.page.bringToFront();
          return;
        }
      }
      await this.check();
      await this.control.sleep(500);
    }
    throw new Handoff('等待订单结果超时。请查看当前排队状态或未完成订单，程序不会重提。');
  }
  async offerWaitlist(): Promise<void> {
    this.log('可在当前页面按配置选择候补。候补需由你支付预付款后生效；本版本候补由你在网站提交。');
    await this.page.bringToFront();
  }
}
