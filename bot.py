import asyncio
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

URL = 'https://spmt.icai.org/ICAI/LoginAction_showSlotDetails.action'
TARGETS = [
    ('Delhi', 'Delhi New Delhi', 'Dexit Global Limited'),
    ('Delhi', 'Delhi New Delhi', 'Guru Hargobind Institute of Management and Information Technology'),
    ('Delhi NCR', 'Gurugram', 'Dexit Global Limited'),
]
OUT = Path('diagnostics')
STATE = Path('state/slots.json')

def norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())

def dates():
    # Today through today + 30 days: exactly 31 dates in India time.
    today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    return [f'{d.day:02d}-{months[d.month-1]}-{d.year}' for d in
            (today + timedelta(days=i) for i in range(31))]

def notify(text):
    topic = os.environ.get('NTFY_TOPIC', '').strip() or 'spom-fdb44190ac08f5aa508e6be200579493'
    server = os.environ.get('NTFY_SERVER', 'https://ntfy.sh').strip().rstrip('/') or 'https://ntfy.sh'
    token = os.environ.get('NTFY_TOKEN', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', topic):
        raise RuntimeError('Set NTFY_TOPIC to an unguessable 16-64 character topic using letters, numbers, underscores or hyphens.')
    from urllib.parse import urlparse
    parsed = urlparse(server)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError('NTFY_SERVER must be an HTTPS server URL without credentials, query or fragment.')
    headers = {'Content-Type':'text/plain; charset=utf-8',
               'Title':'SPOM monitor alert', 'Priority':'high',
               'Tags':'calendar', 'Click':URL}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    # Keep each UTF-8 body below ntfy's default 4096-byte message limit.
    chunks, chunk, size = [], [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > 3500:
            chunks.append(''.join(chunk)); chunk, size = [], 0
        chunk.append(char); size += width
    if chunk:
        chunks.append(''.join(chunk))
    for message in chunks:
        req = urllib.request.Request(server + '/' + topic,
            data=message.encode('utf-8'), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
            if result.get('event') != 'message' or not result.get('id'):
                raise ValueError('Unexpected ntfy response')
        except Exception:
            # Avoid printing request URL (the topic acts as a secret).
            raise RuntimeError('ntfy notification failed. Check topic, server, token and rate limits.') from None

async def select_control(page, label, index):
    override = os.environ.get(label.upper().replace(' ', '_') + '_SELECTOR')
    if override:
        return page.locator(override)
    control = page.get_by_label(re.compile(label, re.I))
    if await control.count() == 1:
        return control
    # The supplied screenshot shows Country, State, City, Test Centre in this order.
    controls = page.locator('select:visible')
    if await controls.count() != 4:
        raise RuntimeError('Cannot identify form controls. Inspect diagnostics and set selector overrides.')
    return controls.nth(index)

async def choose(page, label, index, desired):
    control = await select_control(page, label, index)
    deadline = asyncio.get_running_loop().time() + 25
    while True:
        options = await control.locator('option').evaluate_all(
            '(els) => els.map(e => ({text:e.textContent.trim(), value:e.value, disabled:e.disabled}))')
        aliases = {norm(desired)}
        if label == 'City' and desired == 'Delhi New Delhi':
            aliases |= {norm('New Delhi'), norm('Delhi - New Delhi')}
        exact = [o for o in options if not o['disabled'] and norm(o['text']) in aliases]
        # Match a long institute name despite suffix/abbreviation, never pick first arbitrary option.
        if not exact and label == 'Test Centre':
            prefix = 'guruhargobindinstituteofmanagement' if 'Guru' in desired else 'dexitgloballimited'
            exact = [o for o in options if not o['disabled'] and norm(o['text']).startswith(prefix)]
        if len(exact) == 1:
            await control.select_option(value=exact[0]['value'])
            await page.wait_for_timeout(1800)
            return exact[0]['text']
        if len(exact) > 1:
            raise RuntimeError(f'Ambiguous {label}: {desired}. Configure exact portal label in TARGETS.')
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError(f'{label} option not found: {desired}. Available: {[o["text"] for o in options]}')
        await page.wait_for_timeout(700)

async def set_date(page, date):
    override = os.environ.get('EXAM_DATE_SELECTOR')
    field = page.locator(override) if override else page.get_by_label(re.compile('Exam Date', re.I))
    if await field.count() != 1:
        field = page.locator('input:visible').filter(has_not=page.locator('[type=hidden]'))
        candidates = []
        for i in range(await field.count()):
            item = field.nth(i)
            typ = await item.get_attribute('type') or 'text'
            if typ in ('text', 'date'):
                candidates.append(item)
        if len(candidates) != 1:
            raise RuntimeError('Cannot identify Exam Date input; set EXAM_DATE_SELECTOR.')
        field = candidates[0]
    if await field.get_attribute('readonly') is not None:
        # Use the portal's own jQuery datepicker API, if present; no CAPTCHA/login bypass.
        ok = await field.evaluate('''(el, value) => {
            if (window.jQuery && window.jQuery.fn.datepicker && window.jQuery(el).hasClass('hasDatepicker')) {
                window.jQuery(el).datepicker('setDate', value).trigger('change'); return true;
            } return false;
        }''', date)
        if not ok:
            raise RuntimeError('Readonly date field uses an unsupported calendar. Inspect diagnostics.')
    else:
        value = date
        if await field.get_attribute('type') == 'date':
            value = datetime.strptime(date, '%d-%b-%Y').strftime('%Y-%m-%d')
        await field.fill(value)
        await field.dispatch_event('change')
        await field.press('Tab')
    if not await field.input_value():
        raise RuntimeError('Exam Date did not retain selected date.')

async def result(page):
    # Never scan body for "Available slots": the static legend would cause false alerts.
    override = os.environ.get('RESULT_SELECTOR')
    if override:
        regions = page.locator(override)
    else:
        regions = page.locator('table:visible').filter(
            has_text=re.compile(r'batch|session|seat|slot|availability', re.I))
    positives, recognized = [], False
    for i in range(await regions.count()):
        region = regions.nth(i)
        rows = region.locator('tr')
        if not await rows.count():
            rows = region.locator(':scope > *')
        for j in range(await rows.count()):
            row = rows.nth(j)
            text = ' '.join((await row.inner_text()).split())
            if not text or re.search(r'fully booked slots.*available slots', text, re.I):
                continue
            if re.search(r'fully booked|sold out|no (?:available )?slots|not available|unavailable', text, re.I):
                recognized = True
                continue
            if re.search(r'\bavailable\b', text, re.I) and not await row.locator('td').count():
                # Header alone is not a positive result.
                continue
            green = await row.evaluate(r'''el => Array.from(el.querySelectorAll('*')).some(e => {
                const c = getComputedStyle(e); return [c.color,c.backgroundColor].some(v => {
                    const a=v.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                    return a && +a[2]>70 && +a[2]>+a[1]*1.4 && +a[2]>+a[3]*1.4;
                });
            })''')
            book = row.get_by_role('button', name=re.compile(r'^book(?: slot| now)?$', re.I))
            enabled_book = await book.count() == 1 and await book.is_enabled()
            explicit = bool(re.search(r'\bavailable\b|(?:available seats|seats available|vacancies)\s*[:=-]?\s*[1-9]\d*', text, re.I))
            if explicit or enabled_book or (green and await row.locator('td').count() > 0):
                positives.append(text)
                recognized = True
    body = await page.locator('body').inner_text()
    if re.search(r'no (?:records|batches|slots|sessions|data)(?: (?:are )?available| found)?|no result found', body, re.I):
        recognized = True
    if not recognized:
        raise RuntimeError('Result layout unrecognized. This is NOT treated as no availability. Inspect diagnostics; adjust result() or RESULT_SELECTOR.')
    return positives

async def save_diagnostics(page):
    OUT.mkdir(exist_ok=True)
    await page.screenshot(path=str(OUT/'last-page.png'), full_page=True)
    (OUT/'last-page.html').write_text(await page.content(), encoding='utf-8')

async def main():
    OUT.mkdir(exist_ok=True)
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    current, errors, checked = {}, [], 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width':1440, 'height':1000})
        page.set_default_timeout(30000)
        try:
            for state, city, centre in TARGETS:
                await page.goto(URL, wait_until='domcontentloaded', timeout=60000)
                await choose(page, 'Country', 0, 'India')
                await choose(page, 'State', 1, state)
                actual_city = await choose(page, 'City', 2, city)
                actual_centre = await choose(page, 'Test Centre', 3, centre)
                for date in dates():
                    await set_date(page, date)
                    search = page.get_by_role('button', name=re.compile('^Search$', re.I))
                    if await search.count() != 1:
                        search = page.get_by_text('Search', exact=True)
                    # Await actual response to Search, rather than reading stale previous results.
                    async with page.expect_response(lambda r: r.request.method == 'POST' or
                            ('action' in r.url and r.request.resource_type in ('document','xhr','fetch')), timeout=30000) as pending:
                        await search.click()
                    response = await pending.value
                    await response.finished()
                    if response.status >= 400:
                        raise RuntimeError(f'Portal returned HTTP {response.status}; stopping without retries.')
                    await page.wait_for_load_state('domcontentloaded')
                    await page.wait_for_timeout(2000)
                    slots = await result(page)
                    key = '|'.join([state, actual_city, actual_centre, date])
                    checked += 1
                    print(f'{date} | {state} | {actual_centre}: {len(slots)} available rows', flush=True)
                    if slots and slots != old.get(key):
                        notify('SPOM availability detected\n' + key.replace('|','\n') + '\n\n' + '\n'.join(slots) + '\n\nCheck and book manually: ' + URL)
                    current[key] = slots
                    await page.wait_for_timeout(2000)
            await save_diagnostics(page)
        except Exception as exc:
            errors.append(str(exc))
            await save_diagnostics(page)
        finally:
            await browser.close()
    # Preserve unvisited keys on partial failure; persist only successfully processed dates.
    merged = {**old, **current}
    active_dates = set(dates())
    merged = {k:v for k,v in merged.items() if k.split('|')[-1] in active_dates}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(merged, indent=2), encoding='utf-8')
    report = {'checked':checked, 'expected':93, 'errors':errors, 'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat()}
    (OUT/'report.json').write_text(json.dumps(report, indent=2))
    if errors:
        try:
            notify('SPOM monitor needs attention. Checked ' + str(checked) + '/93 combinations.\n' + errors[0][:1500] + '\nOpen GitHub Actions diagnostics. No availability conclusion for unchecked dates.')
        finally:
            raise RuntimeError(errors[0])
    print('Completed all 93 combinations.')

if __name__ == '__main__':
    import sys
    if '--test-notification' in sys.argv:
        notify('SPOM notification test successful. Notification delivery is configured; portal monitoring is not yet validated.')
        print('Test notification accepted by ntfy.')
    else:
        asyncio.run(main())
