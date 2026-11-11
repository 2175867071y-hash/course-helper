"""上传暂停后的手动发送恢复；网页与服务响应均为本地合成数据。"""
import unittest
from unittest.mock import patch

import test_doubao_session as session_tests
from test_doubao_web import FIXTURE
from quiz_ai import QuizAIError, QuizImage
import doubao_web


MANUAL = FIXTURE.replace("c.dataset.uploadStatus='success';", '')
MANUAL = MANUAL.replace("editor.innerText='';", """
 const user=document.createElement('div');user.dataset.messageRole='user';
 user.textContent=editor.innerText;document.querySelector('#responses').append(user);
 history.replaceState(null,'','/chat/local-conversation');
 editor.innerText='';""")


class RecoveryTests(unittest.TestCase):
    # 只复用浏览器环境，避免 unittest 重复发现原模块的测试。
    @classmethod
    def setUpClass(cls):
        session_tests.SessionTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        session_tests.SessionTests.tearDownClass.__func__(cls)

    def setUp(self):
        session_tests.SessionTests.setUp(self)
        self.context.unroute('**/*')
        self.context.route('**/*', lambda r: r.fulfill(body=MANUAL, content_type='text/html'))
        self.updates = []
        self.sent_counts = []

    def run_review(self, html=None):
        if html:
            self.context.unroute('**/*')
            self.context.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        original = doubao_web.exchange
        original_receive = doubao_web.receive_reply
        def short(*args, **kwargs):
            return original(*args, **kwargs, timeout=.6, settle=.05)
        def receive(*args, **kwargs):
            kwargs.update(timeout=1.5, settle=.05)
            return original_receive(*args, **kwargs)
        with patch('doubao_web.exchange', short), patch('doubao_web.receive_reply', receive):
            return self.reviewer.with_updates([dict(id='q', kind='single',
                options=['A', 'B'], answer_field='selected_labels', images=['i'])],
                [QuizImage('i', b'PNG')], self.updates.append)

    def manual_send(self, title, message, choices, **kwargs):
        page = self.context.pages[-1]
        self.assertIn('receive', dict(choices))
        self.assertTrue(self.updates)
        self.assertIn('等待附件上传完成', self.updates[-1]['status'])
        self.assertNotEqual(self.updates[-1]['status'], '正在请求')
        page.get_by_test_id('chat_input_send_button').click()
        self.sent_counts.append(len(page.evaluate('window.sent')))
        return 'receive'

    def test_manually_sent_reply_is_received_without_resending_images(self):
        self.control.ask = self.manual_send
        result = self.run_review()
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(self.sent_counts, [1])

    def test_prose_is_reported_and_reformatted_once_in_same_conversation(self):
        html = MANUAL.replace("pre.textContent=JSON.stringify(",
            "pre.textContent=window.sent.length===1 ? '答案：B。' : JSON.stringify(")
        # 新请求发出后旧回答暂时仍在，必须等到新的用户消息和回复出现。
        html = html.replace("const user=document.createElement('div');",
            "if(window.sent.length>1) await new Promise(r=>setTimeout(r,250)); const user=document.createElement('div');")
        self.control.ask = self.manual_send
        result = self.run_review(html)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertTrue(any('答案：B。' in u.get('raw_reply', '') for u in self.updates))
        self.assertTrue(any('整理' in u['status'] for u in self.updates))

    def test_cancel_keeps_failure_status_and_does_not_send(self):
        def close(*args, **kwargs):
            self.sent_counts.append(len(self.context.pages[-1].evaluate('window.sent')))
            return 'close'
        self.control.ask = close
        with self.assertRaises(QuizAIError):
            self.run_review()
        self.assertEqual(self.sent_counts, [0])
        self.assertIn('等待附件上传完成', self.updates[-1]['status'])
        self.assertEqual(self.updates[-1]['status'].count('豆包复核失败，步骤：'), 1)

    def test_unrelated_conversation_is_not_used_or_sent_a_format_request(self):
        calls = []
        def ask(*args, **kwargs):
            page = self.context.pages[-1]
            calls.append(len(page.evaluate('window.sent')))
            if len(calls) > 1:
                return 'close'
            page.locator('[contenteditable]').fill('different conversation')
            page.evaluate("document.querySelector('#responses').innerHTML = '<div data-message-role=user>unrelated</div><div data-message-role=assistant>答案 B</div>'")
            return 'receive'
        self.control.ask = ask
        with self.assertRaises(QuizAIError):
            self.run_review()
        self.assertEqual(calls, [0, 0])

    def test_failed_reformat_is_not_repeated_and_raw_answer_is_retained(self):
        html = MANUAL.replace("pre.textContent=JSON.stringify(",
            "pre.textContent=true ? '答案：B。' : JSON.stringify(")
        calls = []
        def ask(*args, **kwargs):
            page = self.context.pages[-1]
            calls.append(len(page.evaluate('window.sent')))
            if len(calls) == 1:
                return self.manual_send(*args, **kwargs)
            if len(calls) == 2:
                return 'receive'
            return 'close'
        self.control.ask = ask
        with self.assertRaises(QuizAIError):
            self.run_review(html)
        self.assertEqual(calls, [0, 2, 2])
        self.assertTrue(any('答案：B。' in u.get('raw_reply', '') for u in self.updates))
        self.assertIn('不会自动重发', self.updates[-1]['status'])

    def test_streaming_or_draft_is_not_accepted_or_reformatted(self):
        for change in (
            "document.querySelector('[data-message-role=assistant]').dataset.status='streaming'",
            "document.querySelector('[contenteditable]').innerText='my draft'",
        ):
            with self.subTest(change=change):
                page = self.context.new_page()
                page.goto('https://www.doubao.com/chat/')
                page.locator('[contenteditable]').fill('request_id 必须为：testnonce')
                page.get_by_test_id('chat_input_send_button').click()
                page.locator('[data-status=complete]').wait_for()
                page.evaluate(change)
                with self.assertRaises(QuizAIError):
                    doubao_web.receive_reply(page, 'testnonce', self.control,
                        manual=True, timeout=.3, settle=.05)
                self.assertEqual(len(page.evaluate('window.sent')), 1)
                page.close()

    def test_manual_receive_accepts_stable_answer_with_its_own_copy_control(self):
        def ask(*args, **kwargs):
            choice = self.manual_send(*args, **kwargs)
            page = self.context.pages[-1]
            page.locator('[data-status=complete]').wait_for()
            page.locator('[data-message-role=assistant]').evaluate('''e => {
                e.removeAttribute('data-status');
                const copy=document.createElement('button');copy.textContent='复制';
                e.append(copy);
            }''')
            return choice
        self.control.ask = ask
        self.assertEqual(self.run_review()['answers'][0]['selected_labels'], ['B'])

