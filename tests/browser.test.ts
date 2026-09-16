import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { startDemo, demoConfig } from '../src/demo.js';
import { BrowserAdapter } from '../src/browser.js';
import { run } from '../src/engine.js';
import { Control } from '../src/control.js';

test('browser integration: query → passengers → seat → one submission → payment handoff', async () => {
  const demo = await startDemo();
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => new URL(route.request().url()).origin === demo.url ? route.continue() : route.abort());
    const page = await context.newPage(); await page.goto(demo.url);
    const control = new Control();
    const adapter = new BrowserAdapter(page, demoConfig, control, () => {}, async () => '', true);
    let armed = false;
    const guard = { assertClear: async () => {}, arm: async () => { armed = true; assert.equal(demo.records.submits, 0); } };
    const logs: string[] = [];
    const result = await run(demoConfig, adapter, guard, control, { dryRun: false, now: true, log: m => logs.push(m) });
    assert.equal(result, 'payment', logs.join('\n')); assert.equal(armed, true);
    assert.equal(demo.records.submits, 1); assert.equal(demo.records.payments, 0);
    assert.match(page.url(), /\/otn\/payOrder\/init$/);
  } finally { await browser.close(); await new Promise<void>(resolve => demo.server.close(() => resolve())); }
});

test('browser integration: dry run makes no reservation and does not leave query page', async () => {
  const demo = await startDemo(); const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage(); await page.goto(demo.url);
    const control = new Control();
    const adapter = new BrowserAdapter(page, demoConfig, control, () => {}, async () => '', true);
    const guard = { assertClear: async () => { throw new Error('must not be called'); }, arm: async () => { throw new Error('must not be called'); } };
    assert.equal(await run(demoConfig, adapter, guard, control, { dryRun: true, log: () => {} }), 'dry-run');
    assert.equal(demo.records.submits, 0); assert.equal(demo.records.payments, 0); assert.equal(new URL(page.url()).pathname, '/');
  } finally { await browser.close(); await new Promise<void>(resolve => demo.server.close(() => resolve())); }
});

test('browser integration: seat tampering is caught before submit', async () => {
  const demo = await startDemo(); const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage(); await page.goto(demo.url);
    const adapter = new BrowserAdapter(page, demoConfig, new Control(), () => {}, async () => '', true);
    const offers = await adapter.query(); await adapter.book(offers[0]); await adapter.fillOrder(offers[0]);
    await page.locator('#seatType_1').selectOption('1');
    await assert.rejects(() => adapter.submit(), /席别或票种/); assert.equal(demo.records.submits, 0);
  } finally { await browser.close(); await new Promise<void>(resolve => demo.server.close(() => resolve())); }
});
