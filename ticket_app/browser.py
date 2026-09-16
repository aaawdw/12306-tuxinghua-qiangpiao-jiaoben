from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlparse

from .config import SEATS
from .engine import Handoff

ORIGIN = "https://kyfw.12306.cn"


def norm(text):
    return re.sub(r"\s+", "", text)


class BrowserAdapter:
    def __init__(self, page, config, control, demo=False, emit=None):
        self.page, self.config, self.control, self.demo = page, config, control, demo
        self.dialog_seen = False
        self.offer = None
        self.emit = emit or (lambda *_: None)
        self.stage = "初始化"
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(30000)
        page.on("dialog", self._dialog)

    async def _dialog(self, dialog):
        self.dialog_seen = True
        await dialog.dismiss()

    async def visible(self, locator):
        return await locator.first.is_visible()

    async def check(self):
        await self.control.checkpoint()
        if self.page.is_closed():
            raise Handoff("浏览器已关闭，请检查未完成订单。")
        u = urlparse(self.page.url)
        if not self.demo and f"{u.scheme}://{u.netloc}" != ORIGIN:
            raise Handoff("页面已离开 12306 购票站点，请人工检查。")
        if self.dialog_seen:
            raise Handoff("网站出现了未识别的确认框，已停止自动操作，请人工处理。")
        if await self.visible(self.page.locator(".nc-container:visible, .nc_wrapper:visible, #slide-passcode:visible, #J-login:visible")):
            raise Handoff("网站需要登录或验证码核验，请在浏览器中处理。")

    async def verify_query(self):
        await self.check()
        await self.ensure_query_page()
        p, c = self.page, self.config
        if (await p.locator("input#fromStationText").input_value() != c["from"]
                or await p.locator("input#toStationText").input_value() != c["to"]
                or (await p.locator("input#train_date").input_value())[:10] != c["travelDate"]):
            raise Handoff("页面的车站或日期与配置不一致，停止操作。请勿在运行中修改浏览器查询条件。")
        login = p.locator("#login_user")
        if not self.demo and await self.visible(login) and norm(await login.inner_text()) == "登录":
            raise Handoff("查询页面仍显示未登录，请重新登录后再运行。")
        for name in ("autoSubmit", "partSubmit", "auto_query"):
            checkbox = p.locator(f"input#{name}")
            if await checkbox.count() and await checkbox.is_checked():
                if not await self.visible(checkbox):
                    raise Handoff("网站自身的自动提交或自动查询选项已开启，请在网站关闭后重试。")
                await checkbox.uncheck()

    async def ensure_query_page(self):
        if await self.visible(self.page.locator('#qr_submit_id')):
            raise Handoff("浏览器已在订单确认弹窗，自动查询已停止。请核对当前订单后在浏览器继续，程序不会重复预订。")
        if (urlparse(self.page.url).path.startswith('/otn/confirmPassenger/')
                or await self.visible(self.page.locator('#submitOrder_id'))):
            raise Handoff("浏览器已进入乘客确认页，自动查询已停止。请处理当前页面，程序不会返回查询或重复预订。")
        if not await self.visible(self.page.locator('#query_ticket')):
            raise Handoff("当前不是可识别的余票查询页，请检查浏览器；不会自动重试下单。")

    async def prepare_for_sale(self):
        self.stage = "开售前检查"
        await self.verify_query()
        try:
            await self.page.wait_for_function("""() => {
                const b = document.querySelector('#query_ticket');
                return b && !b.classList.contains('btn-disabled') && !b.disabled;
            }""", timeout=3000)
        except Exception:
            await self.check()
            raise Handoff("临近开售时查询页面仍未就绪，请检查登录和页面提示。尚未预订或提交订单。") from None

    async def query(self):
        self.stage = "余票查询"
        try:
            return await self._query()
        except Handoff:
            raise
        except Exception:
            await self.check()
            await self.ensure_query_page()
            raise

    async def _query(self):
        await self.verify_query()
        button = self.page.locator("#query_ticket")
        if "btn-disabled" in (await button.get_attribute("class") or ""):
            self.emit("log", "网站正在加载查询结果，等待查询按钮恢复。")
            await self.page.wait_for_function("""() => {
                const b = document.querySelector('#query_ticket');
                return b && !b.classList.contains('btn-disabled') && !b.disabled;
            }""", timeout=20000)
            await self.verify_query()
        # Observe this round's table updates before clicking. Animation frames
        # may stop in a background window and don't prove results are fresh.
        watch = await self.page.locator('#queryLeftTable').evaluate_handle('''e => {
            const w = {changed:false};
            w.observer = new MutationObserver(() => w.changed = true);
            w.observer.observe(e, {childList:true, subtree:true, characterData:true});
            return w;
        }''')
        try:
            if not await self.query_response(button, watch):
                return []
        finally:
            try:
                await watch.evaluate('w => w.observer.disconnect()')
                await watch.dispose()
            except Exception:
                pass  # Navigation already discards the old document/observer.
        await self.check()
        rows = await self.page.locator('#queryLeftTable tr[id^="ticket_"]').evaluate_all("""els => els.filter(e => e.offsetParent !== null).map(e => ({
            rowId: e.id, train: e.querySelector('a.number')?.textContent?.trim() || '',
            bookable: [...e.querySelectorAll('a.btn72')].some(a => a.textContent?.trim() === '预订' && !a.classList.contains('btn-disabled')),
            cells: [...e.querySelectorAll('td[id]')].map(td => ({id:td.id,text:td.textContent?.trim()||''}))
        }))""")
        offers = []
        for pref in self.config["preferences"]:
            for row in rows:
                if row["train"] != pref["train"]:
                    continue
                cell = next((cell for cell in row["cells"] if cell["id"].startswith(SEATS[pref["seat"]])), None)
                if cell:
                    offers.append(pref | {"rowId": row["rowId"], "availability": cell["text"], "bookable": row["bookable"]})
        return offers

    async def query_response(self, button, watch):
        def is_query(response):
            return re.fullmatch(r"/otn/leftTicket/query[A-Za-z]*", urlparse(response.url).path) and response.request.method == "GET"
        async with self.page.expect_response(is_query, timeout=20000) as pending:
            await button.click()
        response = await pending.value
        if response.status in (403, 429):
            raise Handoff("网站限制了当前请求，已停止自动查询，请按页面提示处理。")
        if not response.ok:
            raise RuntimeError("query response error")
        data = await response.json()
        payload = data.get('data') if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get('status') is not True
                or not isinstance(payload, dict) or not isinstance(payload.get('result'), list)):
            raise Handoff("网站未返回有效查询结果，可能需要核验，请检查浏览器。")
        await self.check()
        if not payload['result']:
            return False  # An empty response must never reuse old visible rows.
        end = time.monotonic() + 20
        while time.monotonic() < end:
            await self.check()
            if await watch.evaluate('''w => {
                const button = document.querySelector('#query_ticket');
                return w.changed && button && !button.disabled && !button.classList.contains('btn-disabled');
            }'''):
                return True
            await self.control.sleep(0.05)
        raise RuntimeError('query results did not refresh')

    async def book(self, offer):
        self.stage = "打开乘客确认页"
        await self.check()
        row = self.page.locator("#queryLeftTable tr").filter(has=self.page.locator("a.number").filter(has_text=re.compile("^" + re.escape(offer["train"]) + "$")))
        if await row.count() != 1 or await row.get_attribute("id") != offer["rowId"]:
            raise Handoff("目标车次发生变化，请人工检查。")
        await row.get_by_role("link", name="预订", exact=True).click()
        await self.wait_for_order()

    async def wait_for_order(self, timeout=20):
        # A list containing floated children may have zero height even when its
        # checkboxes are visible. Test the controls, not the list's bounding box.
        end = time.monotonic() + timeout
        while True:
            await self.check()
            info_ready = await self.visible(self.page.locator("#ticket_tit_id"))
            passengers_ready = await self.visible(self.page.locator(
                '#normal_passenger_id input[type="checkbox"]:visible'))
            submit_ready = await self.visible(self.page.locator("#submitOrder_id"))
            if info_ready and passengers_ready and submit_ready:
                return
            if time.monotonic() >= end:
                break
            await self.control.sleep(0.2)
        if info_ready or urlparse(self.page.url).path.startswith("/otn/confirmPassenger/"):
            if not passengers_ready:
                raise Handoff("已进入乘客确认页，但乘车人名单尚未加载或没有可见的乘客选框。请检查页面提示和已保存的乘车人；尚未提交订单。")
            raise Handoff("已进入乘客确认页，但列车信息或提交按钮尚未就绪，请检查浏览器；尚未提交订单。")
        raise Handoff("预订后未识别到乘客确认页，请检查登录、验证或余票提示；尚未提交订单。")

    async def verify_trip(self, offer):
        # ticketInfo_id holds passenger rows; ticket_tit_id holds the journey.
        info = self.page.locator("#ticket_tit_id")
        if not await self.visible(info):
            raise Handoff("无法识别订单行程区域，请人工核对。")
        raw = await info.inner_text()
        text = norm(raw)
        c = self.config
        y, m, d = c["travelDate"].split("-")
        dates = (c["travelDate"], f"{y}年{m}月{d}日", f"{y}年{int(m)}月{int(d)}日")
        def station(name):
            return re.search(r"(?:^|[^\u3400-\u9fff]|次)" + re.escape(name) + r"(?:站)?(?![\u3400-\u9fff])", raw)
        start, end = station(c["from"]), station(c["to"])
        errors = []
        if not re.search(r"(^|[^A-Z0-9])" + re.escape(offer["train"]) + r"([^A-Z0-9]|$)", raw):
            errors.append(f'车次应为 {offer["train"]}')
        if not start:
            errors.append(f'出发站应为“{c["from"]}”')
        if not end:
            errors.append(f'到达站应为“{c["to"]}”')
        if start and end and start.start() >= end.start():
            errors.append('出发站与到达站顺序不符')
        if not any(re.search(r'(?<!\d)' + re.escape(date) + r'(?!\d)', text) for date in dates):
            errors.append(f'乘车日期应为 {c["travelDate"]}')
        if errors:
            raise Handoff('订单行程核对未通过：' + '；'.join(errors) + f'。页面列车信息：{norm(raw)[:160]}。请核对配置，尚未继续提交。')

    async def select_named(self, select, wanted):
        options = await select.locator("option").evaluate_all("els => els.map(e => ({value:e.value,text:e.textContent||'',disabled:e.disabled}))")
        matches = [o for o in options if not o["disabled"] and re.split(r"[（(]", norm(o["text"]))[0] == wanted]
        if len(matches) != 1:
            raise Handoff(f"订单没有唯一可用的“{wanted}”选项，请人工核对。")
        await select.select_option(matches[0]["value"])

    async def fill_order(self, offer):
        self.stage = "选择乘客与席别"
        await self.check()
        await self.verify_trip(offer)
        p, c = self.page, self.config
        selected = p.locator('#normal_passenger_id input[type="checkbox"]:checked, #dj_passenger_id input[type="checkbox"]:checked')
        while await selected.count():
            await self.check()
            await selected.first.uncheck()
        for person in c["passengers"]:
            await self.check()
            labels = p.locator("#normal_passenger_id label:visible")
            matches = []
            for i in range(await labels.count()):
                label = labels.nth(i)
                if norm(await label.inner_text()) in (person["name"], person["name"] + "(学生)", person["name"] + "（学生）"):
                    matches.append(label)
            if len(matches) != 1:
                raise Handoff("无法唯一匹配配置中的乘客，请在浏览器核对名单。")
            identifier = await matches[0].get_attribute("for")
            if not identifier:
                raise Handoff("乘客选择控件已变化，请人工处理。")
            # JSON's default \uXXXX escapes are not CSS Unicode escapes.
            await p.locator(f"input[id={json.dumps(identifier, ensure_ascii=False)}]").check()
        seats = p.locator('select[id^="seatType_"]')
        tickets = p.locator('select[id^="ticketType_"]')
        if await seats.count() != len(c["passengers"]) or await tickets.count() != len(c["passengers"]):
            raise Handoff("订单人数与配置不一致，请人工核对。")
        for i in range(await seats.count()):
            await self.check()
            suffix = (await seats.nth(i).get_attribute("id")).removeprefix("seatType_")
            name_input = p.locator(f'input[id="passenger_name_{suffix}"]')
            if await name_input.count() != 1:
                raise Handoff("无法验证订单行中的乘客姓名，请人工核对。")
            name = (await name_input.input_value()).strip()
            person = next((v for v in c["passengers"] if v["name"] == name), None)
            if not person:
                raise Handoff("订单中出现未配置的乘客，已停止。")
            # Changing ticket type rebuilds the seat options on the real site.
            await self.select_named(p.locator(f'select[id="ticketType_{suffix}"]'), person["ticket"])
            await self.select_named(seats.nth(i), offer["seat"])
        if c["berth"] in ("下铺优先", "必须下铺") and "卧" in offer["seat"]:
            raise Handoff(f'你设置了“{c["berth"]}”，请在当前页面人工选铺并提交。程序不会把铺位偏好当作已满足。')
        self.offer = offer
        await self.verify_trip(offer)
        await self.verify_people(offer)

    async def verify_people(self, offer):
        p, c = self.page, self.config
        names = p.locator('input[id^="passenger_name_"]')
        if await names.count() != len(c["passengers"]) or await p.locator('select[id^="seatType_"]').count() != len(c["passengers"]):
            raise Handoff("订单人数发生变化，已停止提交。")
        seen = set()
        for i in range(await names.count()):
            name = (await names.nth(i).input_value()).strip()
            person = next((v for v in c["passengers"] if v["name"] == name), None)
            if not person or name in seen:
                raise Handoff("订单乘客与配置不一致，已停止提交。")
            seen.add(name)
            suffix = (await names.nth(i).get_attribute("id")).removeprefix("passenger_name_")
            seat = await p.locator(f'select[id="seatType_{suffix}"] option:checked').inner_text()
            ticket = await p.locator(f'select[id="ticketType_{suffix}"] option:checked').inner_text()
            if re.split(r"[（(]", norm(seat))[0] != offer["seat"] or re.split(r"[（(]", norm(ticket))[0] != person["ticket"]:
                raise Handoff("已选择的席别或票种发生变化，已停止提交。")

    async def submit(self):
        self.stage = "提交订单并等待确认弹窗"
        await self.check()
        if not self.offer:
            raise Handoff("尚未完成订单核对，无法提交。")
        await self.verify_trip(self.offer)
        await self.verify_people(self.offer)
        await self.page.locator("#submitOrder_id").click()
        try:
            await self.page.locator("#qr_submit_id").wait_for(state="visible", timeout=20000)
        except Exception:
            raise Handoff("提交后未能识别确认窗口，请检查页面和未完成订单，程序不会重提。") from None
        await self.check()
        text = await self.page.locator("#qr_submit_id").evaluate("e => e.closest('.up-box, .dhtmlx_window_active, .dhtmlx_window_inactive')?.innerText")
        if not text:
            raise Handoff("无法识别订单确认窗口，请人工确认。")
        if not self.config["allowNoSeat"] and "无座" in text:
            raise Handoff("确认窗口包含无座提示，已停止，请人工处理。")
        if re.search("部分提交|部分乘客|部分无票", text):
            raise Handoff("网站提示部分乘客或部分提交，请人工核对。")
        self.stage = "确认弹窗选座"
        self.emit("state", "confirming")
        self.emit("log", "已进入订单确认弹窗，正在处理选座。")
        await self.select_positions()
        self.stage = "等待网站允许确认"
        self.emit("log", "选座处理完成，等待网站确认按钮可用。")
        await self.wait_for_confirm_button()
        await self.check()
        await self.verify_trip(self.offer)
        await self.verify_people(self.offer)
        await self.control.checkpoint()
        await self.page.locator("#qr_submit_id").click()

    async def select_positions(self):
        if self.offer and "卧" in self.offer["seat"]:
            await self.select_berths()
            return
        preference = self.config.get("seatPosition", "自动分配")
        seats = self.page.locator('#id-seat-sel div.sel-item a:visible')
        # Zero choices means automatic allocation; partial choices are rejected
        # by the site. Clear any preselected positions before applying a policy.
        selected = self.page.locator('#id-seat-sel div.sel-item a.cur:visible')
        for _ in range(await selected.count()):
            await self.check()
            await selected.first.click()
        if await selected.count():
            raise Handoff("无法清除已有选座，请在当前弹窗处理。")
        if preference == "自动分配":
            self.emit("log", "座位由 12306 自动分配，无需逐个点击座位字母。")
            return
        choices = []
        for i in range(await seats.count()):
            seat = seats.nth(i)
            label = norm(await seat.inner_text())
            if label not in "ABCDF" or len(label) != 1:
                continue
            if await seat.get_attribute('aria-disabled') == 'true' or 'disabled' in (await seat.get_attribute('class') or ''):
                continue
            choices.append((label, seat))
        count = len(self.config['passengers'])
        if len(choices) < count:
            self.emit("log", "网站未提供足够的可识别选座位置，改由 12306 自动分配。")
            return
        # A/F are window seats; C/D are aisle seats for the supported layouts.
        wanted = 'AF' if preference == '靠窗优先' else 'CD'
        choices.sort(key=lambda item: item[0] not in wanted)
        for _, seat in choices[:count]:
            await self.check()
            await seat.click()
            if 'cur' not in (await seat.get_attribute('class') or '').split():
                raise Handoff("网站未确认选座操作，请在当前弹窗核对；不会重复提交订单。")
        if await selected.count() != count:
            raise Handoff("选座数量与乘客人数不一致，请在当前弹窗处理；不会继续确认。")
        self.emit("log", f"已按{preference}选择 {count} 个位置；最终座位以 12306 分配为准。")

    async def select_berths(self):
        preference = self.config.get('berth', '不限')
        if preference == '不限':
            self.emit('log', '卧铺由 12306 自动分配。')
            return
        if preference != '中铺优先':
            raise Handoff('当前铺位要求需要人工处理，请在确认弹窗核对。')
        panel = self.page.locator('#id-bed-sel')
        if not await self.visible(panel):
            self.emit('log', '当前车次或账号未提供选铺，改由 12306 自动分配。')
            return

        async def counts():
            result = {}
            for name in ('x_no', 'z_no', 's_no'):
                counter = panel.locator(f'#{name}')
                if await counter.count() != 1:
                    raise Handoff('无法识别选铺数量，请在确认弹窗人工核对。')
                value = norm(await counter.inner_text())
                if not value.isdigit() or not 0 <= int(value) <= 9:
                    raise Handoff('选铺数量异常，请在确认弹窗人工核对。')
                result[name] = int(value)
            return result

        async def adjust(name, action, expected):
            # Use the visible site's +/- control, never modify counters or call
            # the site's internal function directly.
            controls = panel.locator('[onclick*="numSet"]:visible')
            pattern = re.compile(r"numSet\(\s*(['\"])([^'\"]+)\1\s*,\s*(['\"])" + name + r"\3\s*\)")
            matches = []
            for i in range(await controls.count()):
                control = controls.nth(i)
                match = pattern.search(await control.get_attribute('onclick') or '')
                if match and (match[2] == 'add') == (action == 'add'):
                    matches.append(control)
            if len(matches) != 1:
                raise Handoff('无法唯一识别选铺加减按钮，请在确认弹窗人工处理。')
            await self.check()
            await matches[0].click()
            if (await counts())[name] != expected:
                raise Handoff('网站未确认选铺数量变化，请在确认弹窗核对；不会继续确认。')

        current = await counts()
        # Start from no preferences so the site never receives a mixed or
        # partially selected request left over from manual interaction.
        for name, value in current.items():
            for expected in range(value - 1, -1, -1):
                await adjust(name, 'sub', expected)
        if not await self.visible(panel.locator('#mid_bed')):
            self.emit('log', '该席别未提供中铺选项，改由 12306 自动分配。')
            return
        count = len(self.config['passengers'])
        for expected in range(1, count + 1):
            await adjust('z_no', 'add', expected)
        if await counts() != {'x_no': 0, 'z_no': count, 's_no': 0}:
            raise Handoff('选铺数量与乘客人数不一致，请在确认弹窗人工核对。')
        self.emit('log', f'已申请 {count} 个中铺偏好；余票无法满足时由 12306 自动分配。')

    async def wait_for_confirm_button(self, timeout=30):
        end = time.monotonic() + timeout
        button = self.page.locator('#qr_submit_id')
        while True:
            await self.check()
            if await self.visible(button):
                classes = (await button.get_attribute('class') or '').split()
                ready = ('btn92s' in classes) if not self.demo else ('btn92' not in classes or 'btn92s' in classes)
                if ready and await button.is_enabled() and await button.get_attribute('aria-disabled') != 'true':
                    return
            if time.monotonic() >= end:
                raise Handoff("确认按钮仍在倒计时、排队或不可用，请查看弹窗提示；程序不会再次提交。")
            await self.control.sleep(0.2)

    async def wait_for_payment(self):
        self.stage = "等待待支付页"
        end = time.monotonic() + 120
        while time.monotonic() < end:
            await self.control.checkpoint()
            if self.page.is_closed():
                raise Handoff("浏览器已关闭，请查看未完成订单。")
            url = urlparse(self.page.url)
            if (self.demo or f"{url.scheme}://{url.netloc}" == ORIGIN) and url.path.startswith("/otn/payOrder/init"):
                if re.search("席位已锁定|支付剩余时间|网上支付|立即支付|待支付", await self.page.locator("body").inner_text()):
                    await self.page.bring_to_front()
                    return
            await self.check()
            await self.control.sleep(0.5)
        raise Handoff("等待订票结果超时，请查看排队状态或未完成订单。程序不会重提。")
