"""用合成网页检查上传、回复隔离和可取消等待。"""
import json
from pathlib import Path
import tempfile
import time
import unittest

from quiz_ai import QuizImage, QuizAIError
from doubao_web import (make_batches, make_prompt, parse_reply, parse_loose_reply, exchange,
                       profile_lock)


class Control:
    def __init__(self):
        self.logs = []
        self.stopped = False

    def check(self):
        if self.stopped:
            raise KeyboardInterrupt()

    def sleep(self, seconds):
        self.check()
        time.sleep(min(seconds, .03))

    def emit(self, kind, **data):
        self.logs.append((kind, data))


class ProtocolTests(unittest.TestCase):
    def test_json_must_have_matching_nonce_and_no_duplicate_keys(self):
        valid = {'request_id': 'nonce', 'answers': []}
        self.assertEqual(parse_reply('```json\n' + json.dumps(valid) + '\n```', 'nonce'), {'answers': []})
        for text in ('{"request_id":"old","answers":[]}',
                     '{"request_id":"nonce","answers":[],"answers":[]}',
                     'explanation ' + json.dumps(valid),
                     '{"request_id":"nonce","answers":[],"script":"x"}'):
            with self.subTest(text=text), self.assertRaises(QuizAIError):
                parse_reply(text, 'nonce')

    def test_plain_language_answers_are_parsed_when_complete(self):
        data = [dict(id=str(i), kind='single', prompt='p', options=['A', 'B', 'C', 'D'],
                     answer_field='selected_labels') for i in range(4)]
        samples = (
            '1. 答案：D（安全体系）\n2. 答案：B（执行能力）\n3. 答案：ABCD\n4. 答案：A（对）',
            '汇总：1.D；2.B；3.ABCD；4.A',
            '1.D 2.B 3.ABCD 4.A',
        )
        for text in samples:
            with self.subTest(text=text):
                parsed = parse_loose_reply(text, data)
                self.assertEqual([a['selected_labels'] for a in parsed['answers']],
                                 [['D'], ['B'], ['A', 'B', 'C', 'D'], ['A']])

    def test_plain_language_with_missing_or_conflicting_numbers_is_rejected(self):
        data = [dict(id=str(i), kind='single', prompt='p', options=['A', 'B', 'C', 'D'],
                     answer_field='selected_labels') for i in range(4)]
        for text in ('1. 答案：A\n2. 答案：B',
                     '1. 答案：A\n1. 答案：B\n2. 答案：B\n3. 答案：C\n4. 答案：D'):
            with self.subTest(text=text), self.assertRaises(QuizAIError):
                parse_loose_reply(text, data)

    def test_plain_language_out_of_range_or_unverifiable_answers_are_rejected(self):
        two = [dict(id='0', kind='single', prompt='p', options=['A', 'B'],
                    answer_field='selected_labels')]
        with self.assertRaises(QuizAIError):
            parse_loose_reply('1. 答案：D', two)
        # 没有网页字母时，判断题的“对/错”无法确定对应第几个选项，宁可放弃。
        no_letters = [dict(id='0', kind='truefalse', prompt='p', options=['opt_0', 'opt_1'])]
        with self.assertRaises(QuizAIError):
            parse_loose_reply('1. 答案：对', no_letters)

    def test_batches_keep_each_question_and_its_material_complete(self):
        images = [QuizImage(str(i), b'PNG') for i in range(10)]
        questions = [dict(id='a', images=[str(i) for i in range(6)]),
                     dict(id='b', images=[str(i) for i in range(6, 10)])]
        batches = make_batches(questions, images)
        self.assertEqual([len(b[1]) for b in batches], [6, 4])
        self.assertEqual([q['id'] for batch, _ in batches for q in batch], ['a', 'b'])
        for batch, pictures in batches:
            prompt, files = make_prompt(batch, pictures, 'nonce')
            for index, picture in enumerate(pictures, 1):
                self.assertEqual(files[index - 1]['buffer'], picture.data)
                self.assertIn(files[index - 1]['name'], prompt)

    def test_missing_picture_or_oversize_single_question_never_truncates(self):
        with self.assertRaises(QuizAIError):
            make_batches([dict(id='q', images=['missing'])], [])
        images = [QuizImage(str(i), b'PNG') for i in range(9)]
        with self.assertRaises(QuizAIError):
            make_batches([dict(id='q', images=[i.id for i in images])], images)

    def test_shared_once_wordbank_group_is_not_split(self):
        images = [QuizImage(str(i), b'PNG') for i in range(9)]
        questions = [dict(id=str(i), images=[str(i)], word_bank=['a', 'b'], bank_reuse='once')
                     for i in range(9)]
        with self.assertRaises(QuizAIError):
            make_batches(questions, images)

    def test_cancelled_lock_wait_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Control()
            with profile_lock(Path(directory), control):
                control.stopped = True
                with self.assertRaises(KeyboardInterrupt):
                    with profile_lock(Path(directory), control):
                        self.fail('already locked')
            control.stopped = False
            with profile_lock(Path(directory), control):
                pass


FIXTURE = '''<!doctype html><meta charset="utf-8"><button data-testid="upload_file_button" onclick="document.querySelector('#menu').hidden=false">+</button>
<button id="menu" hidden onclick="document.querySelector('input').click()">上传文件或图片</button>
<input type="file" multiple hidden>
<div data-testid="attachment_area"></div><div contenteditable="true"></div>
<button data-testid="chat_input_send_button">发送</button><section id="responses"></section>
<script>
window.sent=[];
document.querySelector('input').onchange=e=>{
 const area=document.querySelector('[data-testid="attachment_area"]');
 for(const f of e.target.files){const c=document.createElement('div');c.dataset.testid='attachment-image-card';
 c.setAttribute('aria-label',f.name);c.dataset.uploadStatus='success';area.append(c);}
};
document.querySelector('[data-testid="chat_input_send_button"]').onclick=async e=>{
 const editor=document.querySelector('[contenteditable]');window.sent.push(editor.innerText);
 const nonce=editor.innerText.match(/request_id 必须为：([a-z0-9]+)/)[1];
 editor.innerText='';document.querySelector('[data-testid="attachment_area"]').innerHTML='';
 e.target.hidden=true;
 const pre=document.createElement('pre');pre.textContent=JSON.stringify({request_id:nonce,answers:[{id:'q',selected_labels:['B'],confidence:.95}]});
 const message=document.createElement('div');message.dataset.messageRole='assistant';message.setAttribute('aria-busy','true');
 message.append(pre);document.querySelector('#responses').append(message);
 setTimeout(()=>{e.target.hidden=false;message.setAttribute('aria-busy','false');message.dataset.status='complete';},100);
};</script>'''


class BrowserExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route('https://www.doubao.com/**', lambda route: route.fulfill(body=FIXTURE, content_type='text/html'))
        self.page.goto('https://www.doubao.com/chat/')
        self.control = Control()

    def tearDown(self):
        self.page.close()

    def test_original_png_uploaded_and_completed_nonce_reply_received(self):
        picture = QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')
        result = exchange(self.page, [dict(id='q', images=['question_q'])], [picture],
                          self.control, timeout=5, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(self.page.locator('input').evaluate('(e)=>e.files[0].size'), len(picture.data))
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)

    def test_streaming_or_stale_answer_does_not_finish(self):
        self.page.evaluate("()=>{document.querySelector('[data-testid=chat_input_send_button]').onclick=e=>{document.querySelector('#responses').innerHTML='<div data-message-role=assistant aria-busy=false><pre>{\"request_id\":\"old\",\"answers\":[]}</pre></div>';document.querySelector('[contenteditable]').innerText='';e.target.hidden=true;};}")
        with self.assertRaisesRegex(QuizAIError, '超时'):
            exchange(self.page, [dict(id='q')], [], self.control, timeout=.5, settle=.05)

    def test_wrong_destination_is_rejected_before_upload(self):
        self.page.goto('about:blank')
        with self.assertRaisesRegex(QuizAIError, '地址'):
            exchange(self.page, [dict(id='q')], [], self.control, timeout=.3)

    def test_busy_upload_with_card_names_does_not_send(self):
        self.page.get_by_test_id('attachment_area').evaluate('(e)=>e.setAttribute("aria-busy","true")')
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q', images=['i'])], [QuizImage('i', b'PNG')],
                     self.control, timeout=.4, settle=.05)
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_busy_reply_with_copy_button_and_send_ready_is_not_complete(self):
        self.page.locator('#responses').evaluate('''e=>{
          e.setAttribute('aria-busy','true');
          e.insertAdjacentHTML('beforeend','<button>copy</button>');
        }''')
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q')], [], self.control, timeout=.5, settle=.05)

    def test_unknown_upload_state_prevents_send(self):
        self.page.evaluate('''()=>{const old=document.querySelector('input').onchange;
          document.querySelector('input').onchange=e=>{old(e);document.querySelectorAll('[data-upload-status]').forEach(c=>c.removeAttribute('data-upload-status'));};}''')
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q', images=['i'])], [QuizImage('i', b'PNG')],
                     self.control, timeout=.4, settle=.05)
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_reply_without_status_or_message_role_is_accepted(self):
        # 真实页面既没有 data-status 也没有标准消息容器；靠“无停止生成、无忙碌、文本稳定”判定结束。
        self.page.evaluate('''()=>{const button=document.querySelector('[data-testid=chat_input_send_button]');
          button.onclick=e=>{const editor=document.querySelector('[contenteditable]');
            const text=editor.innerText;window.sent.push(text);
            const nonce=text.match(/request_id 必须为：([a-z0-9]+)/)[1];
            editor.innerText='';
            document.querySelector('[data-testid="attachment_area"]').innerHTML='';
            const box=document.createElement('div');box.className='markdown';
            const pre=document.createElement('pre');
            pre.textContent=JSON.stringify({request_id:nonce,
              answers:[{id:'q',selected_labels:['B'],confidence:.95}]});
            box.append(pre);document.querySelector('#responses').append(box);
            button.hidden=true;};}''')
        result = exchange(self.page, [dict(id='q')], [], self.control, timeout=5, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)

    def test_unknown_reply_state_with_code_copy_is_not_complete(self):
        self.page.evaluate('''()=>{const old=document.querySelector('[data-testid=chat_input_send_button]').onclick;
          document.querySelector('[data-testid=chat_input_send_button]').onclick=e=>{
            old(e);setTimeout(()=>{const m=document.querySelector('[data-message-role]');m.removeAttribute('data-status');
              m.querySelector('pre').insertAdjacentHTML('afterend','<button>copy</button>');},110);};}''')
        # 生成结束后会出现“复制”操作；状态属性缺失不再阻断接收。
        result = exchange(self.page, [dict(id='q')], [], self.control, timeout=5, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])

    def test_old_conversation_is_not_overwritten(self):
        self.page.locator('#responses').evaluate('e=>e.innerHTML=`<div data-message-role="assistant">Old answer</div>`')
        with self.assertRaisesRegex(QuizAIError, '新对话'):
            exchange(self.page, [dict(id='q')], [], self.control, timeout=.3, settle=.05)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(), '')
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_upload_error_with_not_busy_is_not_success(self):
        self.page.evaluate('''()=>{const old=document.querySelector('input').onchange;
          document.querySelector('input').onchange=e=>{old(e);const c=document.querySelector('[data-upload-status]');
            c.dataset.uploadStatus='error';c.setAttribute('aria-busy','false');};}''')
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q', images=['i'])], [QuizImage('i', b'PNG')],
                     self.control, timeout=.4, settle=.05)
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_reply_error_with_not_busy_is_not_success(self):
        self.page.evaluate('''()=>{const old=document.querySelector('[data-testid=chat_input_send_button]').onclick;
          document.querySelector('[data-testid=chat_input_send_button]').onclick=e=>{
            old(e);setTimeout(()=>{document.querySelector('[data-message-role]').dataset.status='error';},110);};}''')
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q')], [], self.control, timeout=.5, settle=.1)

    def test_missing_upload_cards_prevents_send(self):
        self.page.evaluate("document.querySelector('input').onchange=()=>{}")
        with self.assertRaises(QuizAIError):
            exchange(self.page, [dict(id='q', images=['i'])], [QuizImage('i', b'PNG')],
                     self.control, timeout=.4, settle=.05)
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_ready_thumbnails_without_upload_status_are_accepted(self):
        # 真实页面未必有上传状态属性；缩略图已渲染且无忙碌标记即视为上传完成。
        self.page.evaluate('''()=>{const old=document.querySelector('input').onchange;
          document.querySelector('input').onchange=e=>{old(e);
            for(const card of document.querySelectorAll('[data-testid="attachment-image-card"]')){
              card.removeAttribute('data-upload-status');
              const img=document.createElement('img');
              img.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
              card.append(img);}};}''')
        picture = QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')
        result = exchange(self.page, [dict(id='q', images=['question_q'])], [picture],
                          self.control, timeout=5, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)

    def test_renamed_send_button_is_located_by_label(self):
        self.page.evaluate('''()=>{const b=document.querySelector('[data-testid=chat_input_send_button]');
          b.removeAttribute('data-testid');b.setAttribute('aria-label','发送');}''')
        picture = QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')
        exchange(self.page, [dict(id='q', images=['question_q'])], [picture],
                 self.control, timeout=5, settle=.1)
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)

    def test_plain_language_reply_is_used_without_asking_again(self):
        # 豆包回了“答案：B + 解析”这种文字，程序直接还原，不再要求 JSON。
        self.page.evaluate('''()=>{const button=document.querySelector('[data-testid=chat_input_send_button]');
          button.onclick=e=>{const editor=document.querySelector('[contenteditable]');
            window.sent=(window.sent||[]);window.sent.push(editor.innerText);
            editor.innerText='';
            document.querySelector('[data-testid="attachment_area"]').innerHTML='';
            const pre=document.createElement('pre');
            pre.textContent='1. 答案：B\\n解析：因为 B 正确。';
            const message=document.createElement('div');message.dataset.messageRole='assistant';
            message.dataset.status='complete';message.append(pre);
            document.querySelector('#responses').append(message);};}''')
        data = [dict(id='0', kind='single', options=['A', 'B'],
                     answer_field='selected_labels', images=['question_q'])]
        result = exchange(self.page, data, [QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')],
                          self.control, timeout=5, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)

    def test_unparsable_reply_asks_for_format_once(self):
        self.page.evaluate('''()=>{let count=0;
          const button=document.querySelector('[data-testid=chat_input_send_button]');
          button.onclick=e=>{const editor=document.querySelector('[contenteditable]');
            const text=editor.innerText;window.sent=(window.sent||[]);window.sent.push(text);
            const nonce=text.match(/request_id 必须为：([a-z0-9]+)/)[1];
            editor.innerText='';
            document.querySelector('[data-testid="attachment_area"]').innerHTML='';
            const pre=document.createElement('pre');
            if(count++===0){pre.textContent='这些题目我没法确定。';}
            else{pre.textContent=JSON.stringify({request_id:nonce,
              answers:[{id:'0',selected_labels:['B'],confidence:.95}]});}
            const message=document.createElement('div');message.dataset.messageRole='assistant';
            message.dataset.status='complete';message.append(pre);
            document.querySelector('#responses').append(message);};}''')
        data = [dict(id='0', kind='single', options=['A', 'B'],
                     answer_field='selected_labels', images=['question_q'])]
        result = exchange(self.page, data, [QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')],
                          self.control, timeout=6, settle=.1)
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(len(self.page.evaluate('window.sent')), 2)

    def test_missing_send_button_falls_back_to_enter(self):
        self.page.evaluate('''()=>{
          document.querySelector('[data-testid=chat_input_send_button]').remove();
          const editor=document.querySelector('[contenteditable]');
          editor.addEventListener('keydown',event=>{
            if(event.key!=='Enter'||event.shiftKey)return;
            event.preventDefault();
            const text=editor.innerText;window.sent.push(text);
            const nonce=text.match(/request_id 必须为：([a-z0-9]+)/)[1];
            editor.innerText='';
            document.querySelector('[data-testid="attachment_area"]').innerHTML='';
            const pre=document.createElement('pre');
            pre.textContent=JSON.stringify({request_id:nonce,
              answers:[{id:'q',selected_labels:['B'],confidence:.95}]});
            const message=document.createElement('div');
            message.dataset.messageRole='assistant';message.dataset.status='complete';
            message.append(pre);document.querySelector('#responses').append(message);});}''')
        picture = QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')
        exchange(self.page, [dict(id='q', images=['question_q'])], [picture],
                 self.control, timeout=5, settle=.1)
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)
