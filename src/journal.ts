import { createHash } from 'node:crypto';
import { mkdir, open, readFile } from 'node:fs/promises';
import path from 'node:path';
import type { Config, Offer } from './config.js';

export interface SubmissionGuard { assertClear(): Promise<void>; arm(offer: Offer): Promise<void>; }
export function journeyKey(c: Config): string {
  // Changing preferred trains must not permit a second submission for the same journey.
  return createHash('sha256').update(JSON.stringify([c.from, c.to, c.travelDate, c.passengers.map(p => p.name).sort()])).digest('hex').slice(0, 24);
}
export class Journal implements SubmissionGuard {
  readonly file: string;
  constructor(directory: string, c: Config) { this.file = path.join(directory, journeyKey(c) + '.json'); }
  async assertClear(): Promise<void> {
    try { await readFile(this.file); }
    catch (e) { if ((e as NodeJS.ErrnoException).code === 'ENOENT') return; throw e; }
    throw new Error(`该行程已有提交记录，程序不会再次自动提交。请先在 12306 检查未完成订单，按 README 恢复说明处理记录：${this.file}`);
  }
  async arm(offer: Offer): Promise<void> {
    await mkdir(path.dirname(this.file), { recursive: true });
    const file = await open(this.file, 'wx');
    try { await file.writeFile(JSON.stringify({ status: 'submission-started', at: new Date().toISOString(), train: offer.train, seat: offer.seat }, null, 2)); await file.sync(); }
    finally { await file.close(); }
  }
}
