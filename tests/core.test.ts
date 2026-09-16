import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { chooseOffer, configSchema, hasStock, type Offer } from '../src/config.js';
import { demoConfig } from '../src/demo.js';
import { run, type Adapter } from '../src/engine.js';
import { Control } from '../src/control.js';
import { Journal, journeyKey } from '../src/journal.js';
import { queryUrl } from '../src/browser.js';

const offer: Offer = { train: 'G87', seat: '二等座', rowId: 'ticket_1', availability: '有', bookable: true };
test('query URL preserves literal station-code delimiters used by 12306', async () => {
  const url = await queryUrl(demoConfig);
  assert.ok(url.includes(',BXP&'));
  assert.ok(url.includes(',EAY&'));
  assert.ok(!url.includes('%2C'));
  await assert.rejects(() => queryUrl({ ...demoConfig, from: '不存在的测试车站' }));
});
function fake(overrides: Partial<Adapter> = {}) {
  const calls: string[] = [];
  const adapter: Adapter = {
    query: async () => { calls.push('query'); return [offer]; },
    book: async () => { calls.push('book'); }, fillOrder: async () => { calls.push('fill'); },
    submit: async () => { calls.push('submit'); }, waitForPayment: async () => { calls.push('payment'); },
    offerWaitlist: async () => { calls.push('waitlist'); }, ...overrides,
  };
  const guard = { assertClear: async () => { calls.push('guard'); }, arm: async () => { calls.push('arm'); } };
  return { adapter, guard, calls };
}
test('config rejects impossible dates, duplicate people, unknown options, and unapproved standing', () => {
  for (const config of [
    { ...demoConfig, travelDate: '2026-02-30' },
    { ...demoConfig, passengers: [...demoConfig.passengers, ...demoConfig.passengers] },
    { ...demoConfig, preferences: [{ train: 'G87', seat: '无座' }] },
    { ...demoConfig, pollIntervalMs: 1 },
    { ...demoConfig, saleAt: '2026-09-17T09:15:00' },
    { ...demoConfig, passenger: 'typo' },
  ]) assert.equal(configSchema.safeParse(config).success, false);
  assert.equal(configSchema.safeParse(demoConfig).success, true);
});
test('availability distinguishes waitlist, insufficient quantity and discounts', () => {
  assert.equal(hasStock('候补', 1), false); assert.equal(hasStock('无', 1), false);
  assert.equal(hasStock('1', 2), false); assert.equal(hasStock('0', 1), false);
  assert.equal(hasStock('有折', 2), true); assert.equal(hasStock('2', 2), true);
  assert.equal(hasStock('--', 1), false);
});
test('preference order wins over page order and disabled booking is skipped', () => {
  const config = { ...demoConfig, preferences: [{ train: 'G89', seat: '硬卧' as const }, ...demoConfig.preferences] };
  const second = { ...offer, train: 'G89', seat: '硬卧' as const };
  assert.equal(chooseOffer(config, [offer, second]), second);
  assert.equal(chooseOffer(config, [offer, { ...second, bookable: false }]), offer);
});
test('dry run never reserves or submits', async () => {
  const { adapter, guard, calls } = fake();
  assert.equal(await run(demoConfig, adapter, guard, new Control(), { dryRun: true, log: () => {} }), 'dry-run');
  assert.deepEqual(calls, ['query']);
});
test('successful run durably arms before submission, then stops at payment', async () => {
  const { adapter, guard, calls } = fake();
  assert.equal(await run(demoConfig, adapter, guard, new Control(), { dryRun: false, now: true, log: () => {} }), 'payment');
  assert.deepEqual(calls, ['guard', 'query', 'book', 'fill', 'arm', 'submit', 'payment']);
});
test('timeout after submission never retries or cancels', async () => {
  let submits = 0;
  const { adapter, guard } = fake({ submit: async () => { submits++; throw new Error('network timeout'); } });
  const messages: string[] = [];
  assert.equal(await run(demoConfig, adapter, guard, new Control(), { dryRun: false, now: true, log: m => messages.push(m) }), 'handoff');
  assert.equal(submits, 1); assert.ok(messages.some(m => m.includes('不会自动重提')));
});
test('existing submission blocks even querying', async () => {
  const { adapter, guard, calls } = fake();
  guard.assertClear = async () => { throw new Error('already submitted'); };
  assert.equal(await run(demoConfig, adapter, guard, new Control(), { dryRun: false, now: true, log: () => {} }), 'handoff');
  assert.deepEqual(calls, []);
});
test('stop interrupts the sale countdown without queries', async () => {
  const { adapter, guard, calls } = fake();
  const control = new Control();
  const timer = setTimeout(() => { control.stopped = true; }, 25);
  const result = await run({ ...demoConfig, saleAt: new Date(Date.now() + 60000).toISOString() }, adapter, guard, control, { dryRun: false, log: () => {} });
  clearTimeout(timer); assert.equal(result, 'stopped'); assert.deepEqual(calls, ['guard']);
});
test('journal survives restart and preference changes and is exclusive', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'ticket-journal-test-'));
  try {
    const journal = new Journal(directory, demoConfig);
    await journal.assertClear(); await journal.arm(offer);
    await assert.rejects(() => new Journal(directory, demoConfig).assertClear());
    await assert.rejects(() => journal.arm(offer));
    assert.equal(journeyKey(demoConfig), journeyKey({ ...demoConfig, preferences: [{ train: 'G99', seat: '硬座' }] }));
  } finally { await rm(directory, { recursive: true, force: true }); }
});
