"""图片、听力、拖拽在本地真实浏览器中的集成测试；不提交真实课程。"""
import base64
import io
import json
import struct
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import ManualRequired
from quiz_ai import QuizAnswer, QuizAIError
import quiz_ai
from test_quiz_ai import FakeOpener, FakeResponse


def response(answers):
    return FakeResponse(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps({'answers': answers})}}]}).encode())


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(struct.pack('<h', 0) * 800)
    return stream.getvalue()


class MultimediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1200)
        adapter = SimpleNamespace(page=self.page, platform='uai', guard=lambda **kw: None,
            visible_frames=lambda: [self.page.main_frame], videos=lambda: [], start_attempts=set())
        self.course = CourseActivities(adapter, ai_base_url='https://ai.example/v1',
            ai_model='text-model', ai_api_key='TEXT-SECRET')
        # Old releases ignore these, so tests fail on actual missing media behavior.
        self.course.vision_model = 'vision-model'
        self.course.speech_base_url = 'https://speech.example/v1'
        self.course.speech_model = 'transcribe-model'
        self.course.speech_api_key = 'SPEECH-SECRET'

    def tearDown(self):
        self.page.close()

    def load(self, questions, before=''):
        self.page.set_content('<aside>PRIVATE ACCOUNT<img alt="avatar"></aside><main>' +
            ('<section class="passage">' + before + '</section>' if before else '') +
            questions + '<button onclick="document.querySelector(\'output\').textContent=\'Submitted successfully\';'
            'this.dataset.count=+(this.dataset.count||0)+1;">提交</button><output></output></main>')

    def read(self):
        item = self.course._detect()
        self.assertIsNotNone(item)
        return self.course._read_quiz(item)[0]

    def image_question(self):
        return '''<fieldset><legend>Which shape is shown?</legend>
            <svg width="120" height="90"><circle cx="50" cy="45" r="30" fill="red"/></svg>
            <label><input type="radio" name="shape">circle</label>
            <label><input type="radio" name="shape">square</label></fieldset>'''

    def test_image_pixels_sent_to_vision_model_with_no_account_or_media_urls(self):
        self.load(self.image_question())
        opener = FakeOpener(response([{'id': '0', 'selected': [0], 'confidence': .97}]))
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.course.process(), 'working')
        payload = json.loads(opener.request.data)
        self.assertEqual(payload['model'], 'vision-model')
        parts = payload['messages'][1]['content']
        image_parts = [part for part in parts if part['type'] == 'image_url']
        self.assertEqual(len(image_parts), 1)
        png = base64.b64decode(image_parts[0]['image_url']['url'].split(',', 1)[1])
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertNotIn('PRIVATE ACCOUNT', str(payload))
        self.assertEqual(self.page.locator('button').get_attribute('data-count'), '1')
        self.assertEqual(self.course.process(), 'done')

    def test_changed_picture_while_ai_working_discards_answer(self):
        self.load(self.image_question())
        def solve(*args, **kwargs):
            self.page.locator('circle').evaluate("e=>e.setAttribute('fill','blue')")
            return [QuizAnswer('0', [0], .99)]
        with patch('activities.solve_questions', side_effect=solve):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertFalse(self.page.locator('input').first.is_checked())
        self.assertIsNone(self.page.locator('button').get_attribute('data-count'))

    def test_image_only_options_have_distinct_option_indices(self):
        self.load('''<fieldset><legend>Choose the red shape.</legend>
            <label><input type="radio" name="x"><svg width="40" height="40"><rect width="40" height="40" fill="red"/></svg></label>
            <label><input type="radio" name="x"><svg width="40" height="40"><rect width="40" height="40" fill="blue"/></svg></label></fieldset>''')
        questions = self.read()
        self.assertEqual(len(questions[0].options), 2)
        self.assertNotEqual(questions[0].options[0], questions[0].options[1])
        self.assertEqual(len(questions[0].images), 2)
        for image in questions[0].images:
            self.assertIn(image.id, ' '.join(questions[0].options))

    def test_missing_or_oversized_picture_does_not_request_ai(self):
        for media in ('<img src="data:image/png;base64,broken">',
                      '<canvas width="9000" height="5000"></canvas>'):
            self.load('<fieldset>Describe <input>' + media + '</fieldset>')
            with patch('activities.solve_questions') as solve:
                with self.assertRaises(ManualRequired):
                    self.course.process()
            solve.assert_not_called()

    def test_audio_transcribed_once_then_solved_with_separate_credentials(self):
        sound = base64.b64encode(wav_bytes()).decode()
        self.load('<fieldset>What city is mentioned? <input></fieldset>',
            '<p class="directions">Listen to the recording and fill in the blank.</p>'
            '<audio controls src="data:audio/wav;base64,' + sound + '"></audio>')
        requests = []
        class Opener:
            def open(_, request, timeout):
                requests.append(request)
                if request.full_url.endswith('/audio/transcriptions'):
                    return FakeResponse(b'{"text":"We are arriving in London."}')
                return response([{'id': '0', 'filled': {'q0_b0': 'London'}, 'confidence': .98}])
        with patch('urllib.request.build_opener', return_value=Opener()):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].full_url, 'https://speech.example/v1/audio/transcriptions')
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer SPEECH-SECRET')
        self.assertIn(wav_bytes(), requests[0].data)
        self.assertEqual(requests[1].get_header('Authorization'), 'Bearer TEXT-SECRET')
        self.assertIn('We are arriving in London.', requests[1].data.decode())
        self.assertNotIn('data:audio/', requests[1].data.decode())
        self.assertEqual(self.page.locator('input').input_value(), 'London')

    def test_audio_configuration_missing_stops_before_fill(self):
        self.course.speech_api_key = ''
        sound = base64.b64encode(wav_bytes()).decode()
        self.load('<fieldset>Listen and answer <input></fieldset>',
            '<audio controls src="data:audio/wav;base64,' + sound + '"></audio>')
        with patch('urllib.request.build_opener') as network:
            with self.assertRaisesRegex(ManualRequired, '语音|听力|转写'):
                self.course.process()
        network.assert_not_called()
        self.assertEqual(self.page.locator('input').input_value(), '')

    def test_transcription_failure_does_not_guess_from_question_only(self):
        sound = base64.b64encode(wav_bytes()).decode()
        self.load('<fieldset>Listen and answer <input></fieldset>',
            '<audio controls src="data:audio/wav;base64,' + sound + '"></audio>')
        opener = FakeOpener(FakeResponse(b'{"text":""}'))
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ManualRequired, '转写'):
                self.course.process()
        self.assertEqual(self.page.locator('input').input_value(), '')

    def drag_fixture(self, accepts=True):
        action = "event.preventDefault();this.append(document.getElementById(event.dataTransfer.getData('text/plain')));" if accepts else ''
        return '''<fieldset class="drag-question"><legend>Drag the word to its matching definition.</legend>
            <div class="word-bank"><div draggable="true" id="apple" ondragstart="event.dataTransfer.setData('text/plain',this.id)">apple</div>
            <div draggable="true" id="cat" ondragstart="event.dataTransfer.setData('text/plain',this.id)">cat</div></div>
            <div class="drop-zone" aria-label="an animal" style="height:80px;border:1px solid" ondragover="event.preventDefault()" ondrop="''' + action + '''"></div>
            <div class="drop-zone" aria-label="a fruit" style="height:80px;border:1px solid" ondragover="event.preventDefault()" ondrop="''' + action + '''"></div></fieldset>'''

    def test_drag_match_moves_real_elements_and_checks_result_before_submit(self):
        self.load(self.drag_fixture())
        opener = FakeOpener(response([{'id': '0', 'placements': {'q0_t0': 'q0_s1', 'q0_t1': 'q0_s0'}, 'confidence': .99}]))
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('.drop-zone').all_inner_texts(), ['cat', 'apple'])
        self.assertEqual(self.page.locator('button').get_attribute('data-count'), '1')

    def test_drag_without_accepted_drop_never_submits(self):
        self.load(self.drag_fixture(accepts=False))
        opener = FakeOpener(response([{'id': '0', 'placements': {'q0_t0': 'q0_s1', 'q0_t1': 'q0_s0'}, 'confidence': .99}]))
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ManualRequired, '拖|落点'):
                self.course.process()
        self.assertIsNone(self.page.locator('button').get_attribute('data-count'))

    def test_invalid_drag_mapping_rejected_before_any_drag(self):
        self.load(self.drag_fixture())
        opener = FakeOpener(response([{'id': '0', 'placements': {'q0_t0': 'q0_s0', 'q0_t1': 'q0_s0'}, 'confidence': .99}]))
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(ManualRequired):
                self.course.process()
        self.assertEqual(self.page.locator('.drop-zone').all_inner_texts(), ['', ''])

    def test_image_drag_keeps_material_identity_after_moving(self):
        html = self.drag_fixture().replace('>apple</div>',
            '><svg width="40" height="40"><circle cx="20" cy="20" r="18" fill="red"/></svg></div>')
        self.load(html)
        opener = FakeOpener(response([{'id': '0', 'placements': {'q0_t0': 'q0_s1', 'q0_t1': 'q0_s0'}, 'confidence': .99}]))
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('button').get_attribute('data-count'), '1')

    def test_duplicate_drag_ids_pause_with_actionable_message(self):
        self.load(self.drag_fixture().replace('id="cat"', 'id="apple"'))
        with self.assertRaisesRegex(ManualRequired, '编号重复'):
            self.read()

    def test_sort_reorders_real_elements_and_submits_once(self):
        handler = "event.preventDefault();this.before(document.getElementById(event.dataTransfer.getData('text/plain')));"
        parts = [f'<div draggable="true" id="{word}" style="height:50px;border:1px solid" '
            'ondragstart="event.dataTransfer.setData(\'text/plain\',this.id)" '
            f'ondragover="event.preventDefault()" ondrop="{handler}">{word}</div>'
            for word in ('c', 'a', 'b')]
        self.load('<fieldset><legend>Sort letters alphabetically.</legend><div data-sortable>' +
                  ''.join(parts) + '</div></fieldset>')
        opener = FakeOpener(response([{'id': '0', 'placements':
            {'q0_t0': 'q0_s0', 'q0_t1': 'q0_s1', 'q0_t2': 'q0_s2'}, 'confidence': .99}]))
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('[draggable]').all_inner_texts(), ['a', 'b', 'c'])
        self.assertEqual(self.page.locator('button').get_attribute('data-count'), '1')

    def test_picture_in_button_option_is_not_silently_omitted(self):
        self.load('<fieldset><legend>Choose red.</legend><button role="radio" aria-checked="false">'
            '<svg width="40" height="40"><rect width="40" height="40" fill="red"/></svg></button>'
            '<button role="radio" aria-checked="false">Blue</button></fieldset>')
        questions = self.read()
        self.assertEqual(len(questions[0].images), 1)

    def test_unrelated_picture_inside_main_is_not_uploaded(self):
        self.load('<div class="account-card"><svg width="40" height="40"><text>ACCOUNT</text></svg></div>'
                  '<fieldset>Answer <input></fieldset>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '材料区域'):
                self.course.process()
        solve.assert_not_called()

    def test_hidden_custom_player_audio_is_still_transcribed(self):
        sound = base64.b64encode(wav_bytes()).decode()
        self.load('<fieldset>Listen and answer <input></fieldset>',
            '<p>Listen to the recording.</p><audio hidden src="data:audio/wav;base64,' + sound + '"></audio>')
        questions = self.read()
        self.assertEqual(len(questions[0].audio), 1)

    def test_css_background_question_does_not_silently_use_text_only(self):
        self.load('<fieldset>Identify <span style="display:block;width:40px;height:40px;'
                  'background-image:url(data:image/png;base64,broken)"></span><input></fieldset>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '背景'):
                self.course.process()
        solve.assert_not_called()

    def test_diagnostic_counts_media_without_capture_or_api(self):
        from quiz_dom import quiz_report
        self.load(self.image_question())
        with patch('quiz_media.image_bytes') as capture, patch('urllib.request.build_opener') as api:
            report = quiz_report(self.page.main_frame)
        self.assertEqual(report['images'], 1)
        self.assertEqual(report['status'], 'media')
        capture.assert_not_called()
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
