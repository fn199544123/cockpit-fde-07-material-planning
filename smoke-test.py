#!/usr/bin/env python3
"""当前项目独立浏览器验收；依赖 Python Playwright 和 Chromium。
运行：python3 smoke-test.py --url http://127.0.0.1:18707/
仅使用隔离浏览器上下文，不修改用户现有浏览器数据。
"""
import argparse
import csv
import io
import json
import pathlib
import unittest
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:18707/')
ARGS, _ = parser.parse_known_args()
RESULTS = []

class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1100}, accept_downloads=True)
        self.page = self.context.new_page()
        self.errors = []
        self.requests = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.on('request', lambda req: self.requests.append(req.url))
        response = self.page.goto(ARGS.url)
        self.assertEqual(response.status, 200)
        self.page.wait_for_selector('#orders tr')

    def tearDown(self):
        self.assertEqual(self.errors, [], '浏览器脚本错误')
        self.assertTrue(all(u.rstrip('/') == ARGS.url.rstrip('/') for u in self.requests), self.requests)
        self.context.close()

    def state(self):
        return self.page.evaluate('App.getState()')

    def kit(self, oid):
        return self.page.evaluate('(id)=>App.kit(id)', oid)

    def reset_filters(self):
        self.page.locator('#search').fill('')
        self.page.locator('#status-filter').select_option('all')
        self.page.locator('#priority-filter').select_option('all')

    def restock(self, mid, amount):
        self.page.locator(f'[data-action="restock"][data-id="{mid}"]').click()
        self.page.locator('[name="amount"]').fill(str(amount))
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertFalse(self.page.locator('#editor').is_visible())

    def test_01_kit_shortage_and_details(self):
        self.assertEqual(self.kit('DD-001')['rate'], 50)
        self.assertEqual(self.kit('DD-002')['rate'], 100)
        row = self.kit('DD-001')['rows'][1]
        self.assertEqual((row['need'], row['avail'], row['short']), (300, 180, 120))
        self.page.locator('[data-action="detail"][data-id="DD-001"]').first.click()
        self.assertIn('120', self.page.locator('#detail-content').inner_text())
        self.assertIn('50%', self.page.locator('#detail-content').inner_text())
        self.assertTrue(self.page.locator('[data-action="schedule"][data-id="DD-001"]').is_disabled())

    def test_02_priority_reservation_no_double_use(self):
        self.page.locator('#auto-plan').click()
        s = self.state()
        self.assertEqual(s['queue'], ['DD-002'])
        self.assertEqual(self.page.evaluate("App.reserved('WL-001')"), 6000)
        self.assertEqual(self.page.evaluate("App.available('WL-003')"), 80)
        self.assertEqual(self.kit('DD-002')['rate'], 100)
        self.assertAlmostEqual(self.kit('DD-003')['rate'], 2 / 3 * 100)
        self.assertEqual(self.kit('DD-003')['rows'][2]['short'], 20)
        self.page.locator('#auto-plan').click()
        self.assertEqual(self.state()['queue'], ['DD-002'])
        self.assertEqual(self.page.locator('.bar').count(), 1)
        self.assertEqual(self.page.locator('#queue button').count(), 1)

    def test_03_restock_recalculate_cancel_and_persistence(self):
        self.page.locator('#auto-plan').click()
        self.restock('WL-003', 500)
        self.assertEqual(self.kit('DD-003')['rate'], 100)
        self.assertEqual(self.state()['queue'], ['DD-002'], '补料不能隐式排产')
        self.page.locator('#auto-plan').click()
        self.assertEqual(self.state()['queue'], ['DD-002', 'DD-003'])
        self.assertEqual(self.page.evaluate("App.available('WL-001')"), 0)
        self.page.locator('[data-action="cancel"][data-id="DD-002"]').click()
        self.assertEqual(self.page.evaluate("App.available('WL-001')"), 6000)
        self.assertEqual(self.kit('DD-003')['rate'], 100)
        self.assertEqual(self.page.locator('.bar').count(), 1)
        self.page.reload()
        self.assertEqual(self.state()['queue'], ['DD-003'])
        self.assertEqual(self.state()['materials'][2]['stock'], 680)
        keys = self.page.evaluate('Object.keys(localStorage)')
        self.assertTrue(keys and all(k.startswith('development-7-') for k in keys))

    def test_04_new_edit_bom_and_validation(self):
        self.page.locator('#add-order').click()
        self.page.locator('[name="id"]').fill('DD-901')
        self.page.locator('[name="qty"]').fill('2')
        self.page.locator('[name="priority"]').select_option('3')
        self.page.locator('[name="due"]').fill('2026-10-01')
        self.page.locator('[name="bom_WL-001"]').fill('100')
        self.page.locator('[name="bom_WL-002"]').fill('0')
        self.page.locator('[name="bom_WL-003"]').fill('100')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertEqual(len(self.state()['orders']), 5)
        self.assertEqual(self.kit('DD-901')['rows'][1]['short'], 20)
        self.assertEqual(self.kit('DD-901')['rate'], 50)
        self.page.locator('[data-action="edit"][data-id="DD-901"]').click()
        self.page.locator('[name="qty"]').fill('-1')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertTrue(self.page.locator('#editor').is_visible())
        self.assertEqual(self.state()['orders'][-1]['qty'], 2)
        self.page.locator('[name="qty"]').fill('1')
        self.page.locator('[name="bom_WL-001"]').fill('0')
        self.page.locator('[name="bom_WL-003"]').fill('0')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertIn('至少', self.page.locator('#form-error').inner_text())
        self.page.locator('[name="bom_WL-003"]').fill('100')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertEqual(self.kit('DD-901')['rate'], 100)
        self.assertEqual(self.kit('DD-901')['total'], 1)
        self.page.locator('#add-order').click()
        self.page.locator('[name="id"]').fill('DD-901')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertIn('重复', self.page.locator('#form-error').inner_text())
        self.page.locator('[data-close="editor"]').click()
        self.page.reload()
        self.assertEqual(self.state()['orders'][-1]['qty'], 1)
        self.assertEqual(self.state()['orders'][-1]['due'], '2026-10-01')

    def test_05_material_edit_minimum_and_add(self):
        self.page.locator('#auto-plan').click()
        self.page.locator('[data-action="material"][data-id="WL-001"]').click()
        self.page.locator('[name="stock"]').fill('5999')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertTrue(self.page.locator('#editor').is_visible())
        self.assertEqual(self.state()['materials'][0]['stock'], 12000)
        self.page.locator('[name="stock"]').fill('7000')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertEqual(self.page.evaluate("App.available('WL-001')"), 1000)
        self.page.locator('#add-material').click()
        self.page.locator('[name="id"]').fill('WL-901')
        self.page.locator('[name="name"]').fill('演示填料')
        self.page.locator('[name="stock"]').fill('25')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertEqual(self.state()['materials'][-1]['stock'], 25)
        self.page.locator('[data-action="restock"][data-id="WL-901"]').click()
        self.page.locator('[name="amount"]').fill('-5')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.assertEqual(self.state()['materials'][-1]['stock'], 25)
        self.page.locator('[data-close="editor"]').click()
        self.page.locator('#add-order').click()
        self.assertEqual(self.page.locator('[name="bom_WL-901"]').count(), 1)

    def test_06_filter_csv_reset_confirmation(self):
        self.page.locator('#search').fill('DD-002')
        self.page.locator('#priority-filter').select_option('2')
        self.page.locator('#status-filter').select_option('ready')
        self.assertEqual(self.page.locator('#orders tr').count(), 1)
        with self.page.expect_download() as download:
            self.page.locator('#export').click()
        data = pathlib.Path(download.value.path()).read_text(encoding='utf-8-sig')
        rows = list(csv.reader(io.StringIO(data)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], 'DD-002')
        self.assertEqual(rows[1][10], '100%')
        self.reset_filters()
        self.page.locator('#auto-plan').click()
        self.page.locator('#reset').click()
        self.page.locator('[data-close="confirm-reset"]').click()
        self.assertEqual(self.state()['queue'], ['DD-002'])
        self.page.locator('#reset').click()
        self.page.locator('#reset-confirm').click()
        self.assertEqual(self.state()['queue'], [])
        self.assertEqual(self.state()['materials'][2]['stock'], 180)

    def test_07_gantt_queue_link_and_capacity(self):
        self.page.locator('[data-action="edit"][data-id="DD-003"]').click()
        self.page.locator('[name="workshop"]').select_option('1')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.restock('WL-003', 20)
        self.page.locator('#auto-plan').click()
        self.assertEqual(self.state()['queue'], ['DD-002', 'DD-003'])
        times = self.page.evaluate('App.timeline()')
        self.assertEqual(times[0]['start'], 0)
        self.assertAlmostEqual(times[0]['end'], 10 / 3)
        self.assertEqual(times[1]['start'], times[0]['end'])
        self.page.locator('#search').fill('DD-001')
        self.page.locator('.bar[data-id="DD-003"]').click()
        self.assertTrue('selected' in self.page.locator('[data-order="DD-003"]').get_attribute('class'))
        self.assertTrue('active' in self.page.locator('#queue [data-id="DD-003"]').get_attribute('class'))
        self.page.locator('#queue [data-id="DD-002"]').click()
        self.assertTrue('active' in self.page.locator('.bar[data-id="DD-002"]').get_attribute('class'))
        self.page.locator('[data-action="cancel"][data-id="DD-002"]').click()
        self.assertEqual(self.page.evaluate('App.timeline()')[0]['start'], 0)
        self.page.locator('#plan-start').fill('2027-01-01T08:00')
        self.page.locator('#plan-start').press('Tab')
        self.assertEqual(self.state()['start'], '2027-01-01T08:00')
        self.assertIn('交期风险', self.page.locator('#queue').inner_text())

    def test_08_screenshots_mobile_and_no_external_requests(self):
        self.page.locator('#auto-plan').click()
        self.page.wait_for_function("document.querySelector('#notice').textContent === ''")
        self.page.screenshot(path=str(ROOT / 'acceptance-desktop.png'), full_page=True)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.locator('#add-order').click()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertTrue(self.page.locator('[name="qty"]').is_visible())
        self.page.locator('[data-close="editor"]').click()
        self.page.screenshot(path=str(ROOT / 'acceptance-mobile.png'), full_page=True)

    def test_09_due_order_and_id_tiebreak(self):
        # 编辑同优先级订单交期；一键排产必须先排更早交期。
        for oid, due in [('DD-002', '2026-10-02'), ('DD-003', '2026-10-01')]:
            self.page.locator(f'[data-action="edit"][data-id="{oid}"]').click()
            self.page.locator('[name="priority"]').select_option('2')
            self.page.locator('[name="due"]').fill(due)
            self.page.locator('#edit-form button[type="submit"]').click()
        self.page.locator('#auto-plan').click()
        self.assertEqual(self.state()['queue'], ['DD-003'])
        self.page.locator('[data-action="cancel"][data-id="DD-003"]').click()
        self.page.locator('[data-action="edit"][data-id="DD-002"]').click()
        self.page.locator('[name="due"]').fill('2026-10-01')
        self.page.locator('#edit-form button[type="submit"]').click()
        self.page.locator('#auto-plan').click()
        self.assertEqual(self.state()['queue'], ['DD-002'])

class EvidenceResult(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        RESULTS.append({'test': test._testMethodName, 'status': 'passed'})
    def addFailure(self, test, err):
        super().addFailure(test, err)
        RESULTS.append({'test': test._testMethodName, 'status': 'failed', 'error': self._exc_info_to_string(err, test)})
    def addError(self, test, err):
        super().addError(test, err)
        RESULTS.append({'test': str(test), 'status': 'error', 'error': self._exc_info_to_string(err, test)})

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(PlanningTests))
    evidence = {'timestamp': datetime.now(timezone.utc).isoformat(), 'url': ARGS.url, 'browser': 'Playwright Chromium', 'run_location': 'GPU 宿主机，共享项目路径', 'passed': result.wasSuccessful(), 'count': result.testsRun, 'results': RESULTS}
    (ROOT / 'acceptance-results.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
