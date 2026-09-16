import { z } from 'zod';
import { readFile } from 'node:fs/promises';

// Public query-page cell IDs. Order options are matched separately by visible labels.
export const SEATS = {
  '商务座': 'SWZ_', '特等座': 'TZ_', '优选一等座': 'GG_',
  '一等座': 'ZY_', '二等座': 'ZE_', '高级软卧': 'GR_',
  '软卧': 'RW_', '动卧': 'RW_', '一等卧': 'RW_',
  '硬卧': 'YW_', '二等卧': 'YW_', '软座': 'RZ_', '硬座': 'YZ_', '无座': 'WZ_',
} as const;
export type Seat = keyof typeof SEATS;
export const seatNames = Object.keys(SEATS) as [Seat, ...Seat[]];
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, '日期格式应为 YYYY-MM-DD')
  .refine(s => { const d = new Date(s + 'T00:00:00Z'); return !isNaN(+d) && d.toISOString().slice(0, 10) === s; }, '日期不存在');
const station = z.string().trim().min(1).max(30);
export const configSchema = z.object({
  from: station, to: station, travelDate: date,
  saleAt: z.string().datetime({ offset: true, message: '开售时间需含时区，例如 2026-09-17T09:15:00+08:00' }),
  passengers: z.array(z.object({ name: z.string().trim().min(1).max(60), ticket: z.enum(['成人票', '学生票']).default('成人票') }).strict()).min(1).max(9),
  preferences: z.array(z.object({ train: z.string().trim().toUpperCase().regex(/^[GDCZTKYS]?\d{1,5}$/), seat: z.enum(seatNames) }).strict()).min(1).max(30),
  allowNoSeat: z.boolean().default(false),
  berth: z.enum(['不限', '下铺优先', '必须下铺']).default('不限'),
  waitlist: z.enum(['提醒', '关闭']).default('提醒'),
  pollIntervalMs: z.number().int().min(3000).max(60000).default(5000),
  maxRunMinutes: z.number().min(0.1).max(120).default(10),
  browser: z.enum(['chrome', 'msedge', 'chromium']).default('chrome'),
}).strict().superRefine((c, ctx) => {
  if (c.from === c.to) ctx.addIssue({ code: 'custom', message: '出发站和到达站不能相同' });
  if (new Set(c.passengers.map(p => p.name)).size !== c.passengers.length) ctx.addIssue({ code: 'custom', message: '乘客姓名重复；同名乘客请在网站人工处理' });
  if (!c.allowNoSeat && c.preferences.some(p => p.seat === '无座')) ctx.addIssue({ code: 'custom', message: '选择无座需要 allowNoSeat=true' });
  if (new Set(c.preferences.map(p => `${p.train}/${p.seat}`)).size !== c.preferences.length) ctx.addIssue({ code: 'custom', message: '车次和席别组合不能重复' });
  if (c.saleAt.slice(0, 10) > c.travelDate) ctx.addIssue({ code: 'custom', message: '开售日期不能晚于乘车日期' });
});
export type Config = z.infer<typeof configSchema>;
export type Preference = Config['preferences'][number];
export async function readConfig(path: string): Promise<Config> {
  return configSchema.parse(JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, '')));
}
export function normalizedText(s: string): string { return s.replace(/\s+/g, '').trim(); }
export function hasStock(text: string, count: number): boolean {
  const t = normalizedText(text).replace(/折$/, '');
  return t === '有' || (/^\d+$/.test(t) && Number(t) >= count);
}
export type Offer = Preference & { rowId: string; availability: string; bookable: boolean };
export function chooseOffer(config: Config, offers: Offer[]): Offer | undefined {
  for (const pref of config.preferences) {
    const found = offers.find(o => o.train === pref.train && o.seat === pref.seat && o.bookable && hasStock(o.availability, config.passengers.length));
    if (found) return found;
  }
}
