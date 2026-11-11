"""专用浏览器整段流程回归；全部网页由本地路由提供，不连接豆包。"""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright, Error
from doubao_web import DoubaoReviewer, exchange
from quiz_ai import QuizAIError, QuizImage
from test_doubao_web import Control, FIXTURE


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda r: r.fulfill(body=FIXTURE, content_type='text/html'))
        self.control = Control()
        self.prompts = []
        def ask(title, message, choices, **kwargs):
            self.prompts.append((title, message, choices, kwargs, len(self.context.pages)))
            return 'close'
        self.control.ask = ask
        self.launch = SimpleNamespace(launch_persistent_context=lambda *a, **k: self.context)
        self.reviewer = DoubaoReviewer(SimpleNamespace(chromium=self.launch), self.temp.name,
                                      'msedge', self.control)

    def diagnostic(self):
        return json.loads((Path(self.temp.name) / 'doubao-review' / 'last-error.json').read_text('utf-8'))

    def test_empty_home_without_new_chat_button_can_reach_reply(self):
        # 旧实现会强制寻找不存在的“新对话”按钮，空白首页也在 5 秒后失败。
        try:
            result = self.reviewer([{'id': 'q'}], [])
        except QuizAIError as exc:
            self.fail(f'空白首页应直接使用，却在发送前失败：{exc}')
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(self.prompts, [])

    def test_navigation_error_keeps_window_until_ack_and_exposes_safe_stage(self):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda r: r.abort('connectionrefused'))
        with self.assertRaises(QuizAIError) as caught:
            self.reviewer([{'id': 'q'}], [])
        self.assertIn('打开豆包页面', str(caught.exception))
        self.assertIn('ERR_CONNECTION_REFUSED', str(caught.exception))
        self.assertEqual(len(self.prompts), 1)
        self.assertGreater(self.prompts[0][4], 0, '确认前不能关闭失败窗口')
        self.assertEqual(self.prompts[0][3].get('context'), 'quiz')
        record = self.diagnostic()
        self.assertEqual(record['stage'], '打开豆包页面')
        self.assertEqual(record['code'], 'ERR_CONNECTION_REFUSED')
        self.assertEqual(record['sent'], False)
        self.assertEqual(caught.exception.partial_result, {'answers': []})

    def test_launch_error_has_distinct_stage_without_leaking_exception(self):
        def fail(*args, **kwargs):
            raise Error('browserType.launchPersistentContext: secret-token=/profile/private-name')
        self.launch.launch_persistent_context = fail
        with self.assertRaises(QuizAIError) as caught:
            self.reviewer([{'id': 'q'}], [])
        self.assertIn('启动豆包专用浏览器', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))
        self.assertEqual(self.prompts, [])
        self.assertNotIn('private-name', json.dumps(self.diagnostic()))

    def test_draft_is_preserved_and_not_mistaken_for_network_error(self):
        self.context.unroute('**/*')
        html = FIXTURE.replace('<div contenteditable="true"></div>',
                               '<div contenteditable="true">my draft</div>')
        self.context.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        drafts = []
        original_ask = self.control.ask
        def ask(*args, **kwargs):
            drafts.append(self.context.pages[-1].locator('[contenteditable]').inner_text())
            return original_ask(*args, **kwargs)
        self.control.ask = ask
        with self.assertRaises(QuizAIError) as caught:
            self.reviewer([{'id': 'q'}], [])
        self.assertIn('准备空白新对话', str(caught.exception))
        self.assertEqual(drafts, ['my draft'])
        self.assertEqual(self.diagnostic()['sent'], False)

    def test_cleanup_error_does_not_hide_original_failure(self):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda r: r.abort('connectionrefused'))
        real_close = self.context.close
        def bad_close():
            real_close()
            raise Error('cleanup private-token')
        with patch.object(self.context, 'close', bad_close):
            with self.assertRaises(QuizAIError) as caught:
                self.reviewer([{'id': 'q'}], [])
        self.assertIn('ERR_CONNECTION_REFUSED', str(caught.exception))
        self.assertNotIn('private-token', str(caught.exception))

    def test_cancel_during_failure_prompt_still_closes_session(self):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda r: r.abort('connectionrefused'))
        def ask(*args, **kwargs):
            raise KeyboardInterrupt()
        self.control.ask = ask
        with self.assertRaises(KeyboardInterrupt):
            self.reviewer([{'id': 'q'}], [])
        self.assertEqual(self.context.pages, [])

    def test_missing_upload_entry_names_stage_and_does_not_send(self):
        self.context.unroute('**/*')
        html = FIXTURE.replace('data-testid="upload_file_button"', 'data-testid="changed_entry"')
        self.context.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        sent = []
        def ask(*args, **kwargs):
            sent.extend(self.context.pages[-1].evaluate('window.sent'))
            return 'close'
        self.control.ask = ask
        with self.assertRaises(QuizAIError) as caught:
            self.reviewer([{'id': 'q', 'images': ['i']}], [QuizImage('i', b'PNG')])
        self.assertIn('打开附件上传入口', str(caught.exception))
        self.assertEqual(sent, [])
        self.assertFalse(self.diagnostic()['sent'])

    def test_reply_timeout_reports_possible_send_and_never_resends(self):
        self.context.unroute('**/*')
        html = FIXTURE.replace("message.dataset.status='complete'", "message.dataset.status='streaming'")
        self.context.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        sent_counts = []
        def ask(*args, **kwargs):
            sent_counts.append(len(self.context.pages[-1].evaluate('window.sent')))
            return 'close'
        self.control.ask = ask
        def bounded_exchange(*args, **kwargs):
            return exchange(*args, **kwargs, timeout=.5, settle=.05)
        with patch('doubao_web.exchange', bounded_exchange):
            with self.assertRaises(QuizAIError) as caught:
                self.reviewer([{'id': 'q'}], [])
        self.assertIn('等待豆包回答', str(caught.exception))
        self.assertEqual(sent_counts, [1])
        self.assertTrue(self.diagnostic()['sent'])

    def test_login_cancel_does_not_prompt_again_to_close_window(self):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda r: r.fulfill(body='<p>请登录</p>', content_type='text/html'))
        def ask(title, message, choices, **kwargs):
            self.prompts.append(title)
            return 'cancel'
        self.control.ask = ask
        with self.assertRaisesRegex(QuizAIError, '已取消'):
            self.reviewer([{'id': 'q'}], [])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.context.pages, [])
