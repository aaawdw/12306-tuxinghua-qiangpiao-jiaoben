import asyncio
import copy
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from playwright.async_api import async_playwright

from ticket_app.browser import BrowserAdapter
from ticket_app.config import choose_offer, journey_key, load_config, query_url, save_config, validate
from ticket_app.demo import DEMO_CONFIG, DemoServer
from ticket_app.engine import Control, Handoff, Journal, Stopped, run
from ticket_app.worker import Worker

OFFER = {"train": "G87", "seat": "二等座", "rowId": "ticket_demo", "availability": "有", "bookable": True}


class Fake:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def assert_clear(self): self.calls.append("guard")
    def arm(self, _offer): self.calls.append("arm")
    async def query(self): self.calls.append("query"); return [OFFER]
    async def book(self, _offer): self.calls.append("book")
    async def fill_order(self, _offer): self.calls.append("fill")
    async def submit(self):
        self.calls.append("submit")
        if self.fail:
            raise RuntimeError("timeout")
    async def wait_for_payment(self): self.calls.append("payment")


class ConfigTests(unittest.TestCase):
    def test_archive_only_reviewed_journey_and_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder), DEMO_CONFIG)
            other = Journal(Path(folder), DEMO_CONFIG | {'travelDate': '2026-10-01'})
            journal.arm(OFFER)
            other.arm(OFFER)
            previous = journal.path.read_bytes()
            archived = journal.archive_after_review(previous)
            self.assertEqual(archived.read_bytes(), previous)
            self.assertTrue(other.path.exists())
            journal.assert_clear()
            journal.arm(OFFER)
            with self.assertRaises(Handoff):
                journal.assert_clear()

    def test_changed_record_cannot_be_archived_by_old_review(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder), DEMO_CONFIG)
            journal.arm(OFFER)
            previous = journal.path.read_bytes()
            journal.path.write_bytes(previous + b' ')
            with self.assertRaisesRegex(Handoff, '记录已变化'):
                journal.archive_after_review(previous)
            self.assertTrue(journal.path.exists())

    def test_validation(self):
        self.assertEqual(validate(DEMO_CONFIG)["from"], "北京西")
        self.assertEqual(validate(DEMO_CONFIG | {'pollIntervalMs': 100})['pollIntervalMs'], 100)
        invalid = [
            {"travelDate": "2026-02-30"}, {"saleAt": "2026-09-17T09:15:00"},
            {"preferences": [{"train": "G87", "seat": "无座"}]},
            {"pollIntervalMs": 1}, {"maxRunMinutes": float("nan")},
            {"pollIntervalMs": 99}, {"fastStart": 'yes'},
            {"seatPosition": "必须靠窗"},
            {"passengers": DEMO_CONFIG["passengers"] * 2}, {"from": "不存在的车站"},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(DEMO_CONFIG | change)

    def test_station_url(self):
        url = query_url(DEMO_CONFIG)
        self.assertIn(",BXP&", url)
        self.assertIn(",EAY&", url)
        self.assertNotIn("%2C", url)

    def test_preference_and_stock(self):
        second = OFFER | {"train": "K123", "seat": "硬卧"}
        c = DEMO_CONFIG | {"preferences": [{"train": "K123", "seat": "硬卧"}, *DEMO_CONFIG["preferences"]]}
        self.assertEqual(choose_offer(c, [OFFER, second]), second)
        self.assertEqual(choose_offer(c, [OFFER, second | {"bookable": False}]), OFFER)
        for stock in ("候补", "无", "--", "0"):
            self.assertIsNone(choose_offer(DEMO_CONFIG, [OFFER | {"availability": stock}]))
        self.assertIsNone(choose_offer(DEMO_CONFIG | {"passengers": [{}, {}]}, [OFFER | {"availability": "1"}]))

    def test_atomic_config_and_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            file = folder / "config.json"
            save_config(file, DEMO_CONFIG)
            self.assertEqual(load_config(file)["passengers"], DEMO_CONFIG["passengers"])
            before = file.read_bytes()
            with self.assertRaises(ValueError): save_config(file, DEMO_CONFIG | {"from": ""})
            self.assertEqual(before, file.read_bytes())
            journal = Journal(folder, DEMO_CONFIG)
            journal.assert_clear()
            journal.arm(OFFER)
            with self.assertRaises(Handoff): Journal(folder, DEMO_CONFIG).assert_clear()
            with self.assertRaises(Handoff): journal.arm(OFFER)
            self.assertEqual(journey_key(DEMO_CONFIG), journey_key(DEMO_CONFIG | {"preferences": [{"train": "K1", "seat": "硬座"}]}))
            legacy = json.dumps(["北京西", "西安北", "2026-09-30", ["演练乘客"]], ensure_ascii=False, separators=(",", ":"))
            import hashlib
            self.assertEqual(journey_key(DEMO_CONFIG), hashlib.sha256(legacy.encode()).hexdigest()[:24])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def simulate_timing(self, *, interval=100, scheduled=True, rounds=4, fail_first=False):
        clock = [1000.0]
        queries, preparation, events = [], [], []
        class TimedControl(Control):
            async def sleep(self, seconds):
                await self.checkpoint()
                clock[0] += seconds
        class TimedAdapter(Fake):
            async def prepare_for_sale(self):
                preparation.append(clock[0])
            async def query(self):
                queries.append(clock[0])
                clock[0] += 0.4  # Simulate response/DOM latency separately.
                if fail_first and len(queries) == 1:
                    raise RuntimeError('temporary query failure')
                return [OFFER] if len(queries) >= rounds else []
        config = DEMO_CONFIG | {
            'saleAt': datetime.fromtimestamp(1006, timezone.utc).isoformat(),
            'pollIntervalMs': interval, 'maxRunMinutes': 2, 'fastStart': True,
        }
        adapter = TimedAdapter()
        with patch('ticket_app.engine.time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])):
            result = await run(config, adapter, adapter, TimedControl(), lambda *args: events.append(args), now=not scheduled)
        self.assertEqual(result, 'payment', events)
        self.assertEqual(adapter.calls.count('submit'), 1)
        return queries, preparation

    async def test_scheduled_first_query_and_100ms_gap(self):
        queries, preparation = await self.simulate_timing()
        self.assertEqual(preparation, [1001.0])
        self.assertAlmostEqual(queries[0], 1006.0)
        for before, after in zip(queries, queries[1:]):
            self.assertAlmostEqual(after - before, 0.5)  # 400ms response + 100ms wait

    async def test_immediate_mode_keeps_100ms_gap(self):
        queries, preparation = await self.simulate_timing(scheduled=False)
        self.assertEqual(preparation, [])
        self.assertEqual(queries[0], 1000.0)
        self.assertAlmostEqual(queries[1] - queries[0], 0.5)

    async def test_fast_stage_returns_to_configured_interval(self):
        queries, _ = await self.simulate_timing(interval=3000, rounds=28)
        gaps = [round(b - a, 1) for a, b in zip(queries, queries[1:])]
        self.assertEqual(gaps[0], 1.4)
        self.assertEqual(gaps[-1], 3.4)

    async def test_100ms_setting_preserves_query_error_backoff(self):
        queries, _ = await self.simulate_timing(fail_first=True)
        self.assertAlmostEqual(queries[1] - queries[0], 10.4)

    async def test_recovery_waits_for_review_then_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            events = queue.Queue()
            worker = Worker(DEMO_CONFIG, 'live', events, local=folder)
            journal = Journal(Path(folder) / 'attempts', DEMO_CONFIG)
            journal.arm(OFFER)
            previous = journal.path.read_bytes()
            class Page:
                def is_closed(self): return False
            pending = asyncio.create_task(worker.recover_previous_attempt(journal, Page()))
            await asyncio.sleep(0)
            self.assertEqual(events.get_nowait(), ('state', 'recovery'))
            self.assertTrue(journal.path.exists())
            self.assertFalse(pending.done())
            worker.control.recover.set()
            await asyncio.wait_for(pending, 2)
            self.assertFalse(journal.path.exists())
            archive = list((journal.path.parent / 'archive').glob('*.json'))
            self.assertEqual(len(archive), 1)
            self.assertEqual(archive[0].read_bytes(), previous)

    async def test_stopped_recovery_keeps_record(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(DEMO_CONFIG, 'live', queue.Queue(), local=folder)
            journal = Journal(Path(folder) / 'attempts', DEMO_CONFIG)
            journal.arm(OFFER)
            class Page:
                def is_closed(self): return False
            pending = asyncio.create_task(worker.recover_previous_attempt(journal, Page()))
            await asyncio.sleep(0)
            worker.control.stop.set()
            worker.control.recover.set()
            with self.assertRaises(Stopped):
                await pending
            self.assertTrue(journal.path.exists())

    async def test_dry_run(self):
        fake = Fake()
        result = await run(DEMO_CONFIG, fake, fake, Control(), lambda *_: None, dry_run=True)
        self.assertEqual(result, "dry_run")
        self.assertEqual(fake.calls, ["query"])

    async def test_payment_stops_and_arms_first(self):
        fake = Fake()
        result = await run(DEMO_CONFIG, fake, fake, Control(), lambda *_: None, now=True)
        self.assertEqual(result, "payment")
        self.assertEqual(fake.calls, ["guard", "query", "book", "fill", "arm", "submit", "payment"])

    async def test_submit_timeout_never_retries(self):
        fake = Fake(fail=True)
        result = await run(DEMO_CONFIG, fake, fake, Control(), lambda *_: None, now=True)
        self.assertEqual(result, "handoff")
        self.assertEqual(fake.calls.count("submit"), 1)
        self.assertNotIn("payment", fake.calls)

    async def test_stop_and_pause(self):
        control = Control()
        control.paused.set()
        pending = asyncio.create_task(control.checkpoint())
        await asyncio.sleep(.03)
        self.assertFalse(pending.done())
        control.paused.clear()
        await pending
        control.stop.set()
        fake = Fake()
        self.assertEqual(await run(DEMO_CONFIG, fake, fake, control, lambda *_: None, now=True), "stopped")
        self.assertEqual(fake.calls, ["guard"])


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.demo = DemoServer()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        async def restrict(route):
            if route.request.url.startswith(self.demo.url + "/"): await route.continue_()
            else: await route.abort()
        await self.page.route("**/*", restrict)
        await self.page.goto(self.demo.url)
        self.control = Control()
        self.adapter = BrowserAdapter(self.page, copy.deepcopy(DEMO_CONFIG), self.control, demo=True)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        await asyncio.to_thread(self.demo.close)

    async def test_full_flow_zero_payment_clicks(self):
        events = []
        result = await run(DEMO_CONFIG, self.adapter, Fake(), self.control, lambda *args: events.append(args), now=True)
        self.assertEqual(result, "payment", events)
        self.assertEqual(self.demo.records, {"submits": 1, "payments": 0})
        self.assertTrue(self.page.url.endswith("/otn/payOrder/init"))

    async def test_dry_run_does_not_book(self):
        self.assertEqual(await run(DEMO_CONFIG, self.adapter, Fake(), self.control, lambda *_: None, dry_run=True), "dry_run")
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})
        self.assertEqual(self.page.url, self.demo.url + "/")

    async def test_query_stops_when_browser_has_entered_order_page(self):
        await self.page.goto(self.demo.url + '/order')
        with self.assertRaisesRegex(Handoff, '已进入乘客确认页'):
            await self.adapter.query()
        await self.page.locator('#confirmation').evaluate("e => e.style.display='block'")
        with self.assertRaisesRegex(Handoff, '已在订单确认弹窗'):
            await self.adapter.query()
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})

    async def test_query_waits_for_busy_button(self):
        await self.page.locator('#query_ticket').evaluate('''e => {
            e.classList.add('btn-disabled');
            setTimeout(() => e.classList.remove('btn-disabled'), 350);
        }''')
        self.assertEqual((await self.adapter.query())[0]['train'], 'G87')

    async def test_query_does_not_wait_for_animation_frames(self):
        await self.page.evaluate('''() => {
            window.savedRAF = window.requestAnimationFrame;
            document.querySelector('#query_ticket').addEventListener('click', () => {
                window.requestAnimationFrame = () => 0;
            });
        }''')
        try:
            offers = await asyncio.wait_for(self.adapter.query(), 3)
            self.assertEqual(offers[0]['train'], 'G87')
        finally:
            await self.page.evaluate('() => { window.requestAnimationFrame = window.savedRAF; }')

    async def test_query_waits_for_new_rows_instead_of_old_stock(self):
        await self.adapter.query()
        await self.page.evaluate('''() => {
            document.querySelector('#query_ticket').onclick = async () => {
                await fetch('/otn/leftTicket/query');
                setTimeout(() => document.querySelector('#ZE_demo').textContent = '无', 250);
            };
        }''')
        offers = await self.adapter.query()
        self.assertEqual(offers[0]['availability'], '无')
        self.assertIsNone(choose_offer(DEMO_CONFIG, offers))

    async def test_empty_query_response_cannot_reuse_existing_rows(self):
        await self.adapter.query()
        await self.page.route('**/otn/leftTicket/query', lambda route: route.fulfill(
            json={'status': True, 'data': {'result': []}}))
        self.assertEqual(await self.adapter.query(), [])
        self.assertEqual(self.demo.records['submits'], 0)

    async def test_rate_limit_and_invalid_response_stop_without_retry(self):
        for response in ({'status': 429, 'body': 'limited'}, {'json': {'status': True, 'data': None}}):
            requests = []
            async def respond(route):
                requests.append(route.request.url)
                await route.fulfill(**response)
            await self.page.route('**/otn/leftTicket/query', respond)
            result = await run(DEMO_CONFIG, self.adapter, Fake(), self.control, lambda *_: None, now=True)
            self.assertEqual(result, 'handoff')
            self.assertEqual(len(requests), 1)
            await self.page.unroute('**/otn/leftTicket/query', respond)
        self.assertEqual(self.demo.records, {'submits': 0, 'payments': 0})

    async def test_chinese_passenger_id_and_confirmation_countdown(self):
        offers = await self.adapter.query()
        await self.adapter.book(offers[0])
        identifier = await self.page.locator('#normal_passenger_id input').get_attribute('id')
        self.assertIn('演练乘客', identifier)
        await asyncio.wait_for(self.adapter.fill_order(offers[0]), timeout=3)
        await self.page.evaluate('''() => {
            window.earlyClicks = 0;
            document.querySelector('#qr_submit_id').addEventListener('click', e => {
                if (!e.target.classList.contains('btn92s')) window.earlyClicks++;
                e.preventDefault(); e.stopImmediatePropagation();
            }, true);
        }''')
        await self.adapter.submit()
        self.assertEqual(await self.page.evaluate('window.earlyClicks'), 0)
        self.assertIn('btn92s', await self.page.locator('#qr_submit_id').get_attribute('class'))
        self.assertEqual(self.demo.records['submits'], 1)

    async def test_position_preferences_and_automatic_allocation(self):
        await self.page.goto(self.demo.url + '/order')
        await self.page.locator('#confirmation').evaluate("e => e.style.display='block'")
        for preference, expected in [('靠窗优先', ['A']), ('靠过道优先', ['C']), ('自动分配', [])]:
            self.adapter.config['seatPosition'] = preference
            await self.adapter.select_positions()
            self.assertEqual(await self.page.locator('#id-seat-sel a.cur').all_text_contents(), expected)
        self.adapter.config['seatPosition'] = '靠窗优先'
        self.adapter.config['passengers'] = [{}, {}, {}]
        await self.adapter.select_positions()
        self.assertEqual(await self.page.locator('#id-seat-sel a.cur').all_text_contents(), ['A', 'B', 'F'])
        self.adapter.config['passengers'] = [{}] * 6
        await self.adapter.select_positions()
        self.assertEqual(await self.page.locator('#id-seat-sel a.cur').count(), 0)
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})

    async def test_disabled_confirmation_hands_off_without_click(self):
        await self.page.goto(self.demo.url + '/order')
        await self.page.locator('#confirmation').evaluate("e => e.style.display='block'")
        with self.assertRaisesRegex(Handoff, '确认按钮仍在倒计时'):
            await self.adapter.wait_for_confirm_button(timeout=0)
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})

    async def install_bed_controls(self):
        await self.page.evaluate('''() => {
            document.querySelector('#confirmation').insertAdjacentHTML('afterbegin',
                `<div id="id-bed-sel"><div id="low_bed">
                <button onclick="numSet('sub','x_no')">-</button><span id="x_no">0</span><button onclick="numSet('add','x_no')">+</button></div>
                <div id="mid_bed"><button onclick="numSet('sub','z_no')">-</button><span id="z_no">0</span><button onclick="numSet('add','z_no')">+</button></div>
                <div id="high_bed"><button onclick="numSet('sub','s_no')">-</button><span id="s_no">0</span><button onclick="numSet('add','s_no')">+</button></div></div>`);
            window.numSet = (action, id) => {
                const counter = document.getElementById(id);
                counter.textContent = Number(counter.textContent) + (action === 'add' ? 1 : -1);
            };
        }''')

    async def test_middle_berth_full_flow_stops_at_payment(self):
        offer = (await self.adapter.query())[0] | {'seat': '硬卧'}
        self.adapter.config['berth'] = '中铺优先'
        await self.adapter.book(offer)
        await self.install_bed_controls()
        await self.page.evaluate('''() => document.addEventListener('change', e => {
            if (e.target.type === 'checkbox') {
                document.querySelector('#seatType_1').add(new Option('硬卧', '3'));
            }
        })''')
        await self.adapter.fill_order(offer)
        messages = []
        self.adapter.emit = lambda *args: messages.append(args)
        await self.adapter.submit()
        await self.adapter.wait_for_payment()
        self.assertTrue(any('已申请 1 个中铺' in str(m) for m in messages))
        self.assertEqual(self.demo.records, {'submits': 1, 'payments': 0})

    async def test_middle_berth_counts_fallback_and_unresponsive_controls(self):
        await self.page.goto(self.demo.url + '/order')
        await self.install_bed_controls()
        await self.page.locator('#confirmation').evaluate("e => e.style.display='block'")
        self.adapter.offer = OFFER | {'seat': '硬卧'}
        self.adapter.config.update(berth='中铺优先', passengers=[{}, {}])
        await self.page.locator('#x_no').evaluate("e => e.textContent='1'")
        await self.adapter.select_positions()
        self.assertEqual(await self.page.locator('#z_no').inner_text(), '2')
        self.assertEqual(await self.page.locator('#x_no').inner_text(), '0')
        await self.page.locator('#mid_bed').evaluate("e => e.style.display='none'")
        # A fresh dialog for a two-level sleeper has no existing selections.
        await self.page.locator('#z_no').evaluate("e => e.textContent='0'")
        await self.adapter.select_positions()
        self.assertEqual(await self.page.locator('#z_no').inner_text(), '0')
        await self.page.locator('#mid_bed').evaluate("e => e.style.display='block'")
        await self.page.evaluate('window.numSet = () => {}')
        with self.assertRaisesRegex(Handoff, '网站未确认选铺数量变化'):
            await self.adapter.select_positions()
        self.assertEqual(self.demo.records, {'submits': 0, 'payments': 0})

    async def test_seat_tampering(self):
        offers = await self.adapter.query()
        await self.adapter.book(offers[0])
        await self.adapter.fill_order(offers[0])
        await self.page.locator("#seatType_1").select_option("1")
        with self.assertRaisesRegex(Handoff, "席别或票种"):
            await self.adapter.submit()
        self.assertEqual(self.demo.records["submits"], 0)

    async def test_route_mismatch(self):
        offers = await self.adapter.query()
        await self.adapter.book(offers[0])
        await self.page.locator("#ticket_tit_id").evaluate("e => e.textContent='2026-09-30 G87次 北京 → 西安北'")
        with self.assertRaises(Handoff):
            await self.adapter.fill_order(offers[0])
        self.assertEqual(self.demo.records["submits"], 0)

    async def test_trip_diagnostics_keep_exact_station_and_train_boundaries(self):
        await self.page.goto(self.demo.url + '/order')
        self.adapter.config.update({'from':'深圳', 'to':'衡阳', 'travelDate':'2026-09-17'})
        await self.page.locator('#ticket_tit_id').evaluate(
            "e => e.textContent='2026-09-17（周四）K356次 深圳东站（11:26开）—衡阳站（19:38到）'")
        offer = OFFER | {'train': 'K356'}
        with self.assertRaisesRegex(Handoff, '出发站应为“深圳”'):
            await self.adapter.verify_trip(offer)
        self.adapter.config['from'] = '深圳东'
        await self.adapter.verify_trip(offer)
        await self.page.locator('#ticket_tit_id').evaluate("e => e.textContent=e.textContent.replace('K356', 'K356A')")
        with self.assertRaisesRegex(Handoff, '车次应为 K356'):
            await self.adapter.verify_trip(offer)

    async def test_zero_height_passenger_list_still_reaches_payment(self):
        # Reproduce a floated list: its own bounding box is empty, but its
        # passenger controls are visible and usable (no real passenger data).
        async def floated_order(route):
            response = await route.fetch()
            html = await response.text()
            html = html.replace('</style>', '''
                #normal_passenger_id {padding:0; margin:0; list-style:none}
                #normal_passenger_id li {float:left}
                #ticketInfo_id {clear:both}
                </style>''')
            await route.fulfill(response=response, body=html)
        await self.page.route('**/order', floated_order)
        offers = await self.adapter.query()
        await asyncio.wait_for(self.adapter.book(offers[0]), timeout=5)
        self.assertFalse(await self.page.locator('#normal_passenger_id').is_visible())
        self.assertTrue(await self.page.locator('#normal_passenger_id input').is_visible())
        await self.adapter.fill_order(offers[0])
        await self.adapter.submit()
        await self.adapter.wait_for_payment()
        self.assertEqual(self.demo.records, {"submits": 1, "payments": 0})

    async def test_empty_passenger_list_reports_loaded_page_without_submission(self):
        await self.page.goto(self.demo.url + '/order')
        await self.page.locator('#normal_passenger_id').evaluate('e => e.replaceChildren()')
        with self.assertRaisesRegex(Handoff, '已进入乘客确认页.*乘车人名单'):
            await self.adapter.wait_for_order(timeout=0)
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})

    async def test_wrong_page_and_missing_controls_do_not_pass_readiness(self):
        with self.assertRaisesRegex(Handoff, '未识别到乘客确认页'):
            await self.adapter.wait_for_order(timeout=0)
        await self.page.goto(self.demo.url + '/order')
        await self.page.locator('#submitOrder_id').evaluate('e => e.remove()')
        with self.assertRaisesRegex(Handoff, '提交按钮尚未就绪'):
            await self.adapter.wait_for_order(timeout=0)
        self.assertEqual(self.demo.records, {"submits": 0, "payments": 0})

    async def test_ticket_type_rebuilds_seat_options_before_seat_selection(self):
        offers = await self.adapter.query()
        await self.adapter.book(offers[0])
        await self.page.evaluate('''() => document.addEventListener('change', e => {
            if (e.target.id === 'ticketType_1') {
                document.querySelector('#seatType_1').innerHTML =
                    '<option value="1">硬座</option><option value="O">二等座（¥ 100）</option>';
            }
        })''')
        await self.adapter.fill_order(offers[0])
        self.assertEqual(await self.page.locator('#seatType_1').input_value(), 'O')
        await self.adapter.submit()
        await self.adapter.wait_for_payment()
        self.assertEqual(self.demo.records, {"submits": 1, "payments": 0})

    async def test_no_seat_handoff(self):
        offers = await self.adapter.query()
        await self.adapter.book(offers[0])
        await self.adapter.fill_order(offers[0])
        await self.page.locator("#confirmation").evaluate("e => e.insertAdjacentText('afterbegin', '已分配无座')")
        with self.assertRaisesRegex(Handoff, "无座"):
            await self.adapter.submit()
        self.assertEqual(self.demo.records["payments"], 0)
        self.assertFalse(self.page.url.endswith("/otn/payOrder/init"))


class WorkerTests(unittest.TestCase):
    def test_background_worker_preserves_browser_until_close(self):
        with tempfile.TemporaryDirectory() as folder:
            events = queue.Queue()
            worker = Worker(DEMO_CONFIG, "demo", events, local=folder, headless=True)
            worker.start()
            received = []
            try:
                end = time.monotonic() + 25
                result = None
                while time.monotonic() < end:
                    event = events.get(timeout=max(.1, end - time.monotonic()))
                    received.append(event)
                    if event[0] == "result":
                        result = event[1]
                        break
                self.assertEqual(result, "payment", received)
                self.assertTrue(worker.is_alive())
                self.assertTrue((Path(folder) / "run.lock").exists())
                self.assertTrue(any("支付点击 0 次" in str(e) for e in received))
            finally:
                worker.control.close.set()
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertFalse((Path(folder) / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
