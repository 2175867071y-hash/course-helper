"""交叉复核的离线 HTTP 测试；不读取实际凭据，不请求在线服务。"""
import json
import unittest
from unittest.mock import patch

from quiz_ai import QuizAIError, QuizQuestion, QuizBlank, QuizDragItem, QuizImage, QuizAudio, solve_questions
from tests.test_quiz_dom import OfflineResponse


class RoundAI:
    def __init__(self, rounds, after=None):
        self.rounds = iter(rounds)
        self.requests = []
        self.after = after

    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        answer = next(self.rounds)
        if self.after:
            self.after()
        if isinstance(answer, Exception):
            raise answer
        return OfflineResponse(answer)


def choice(identity, labels, confidence=.95):
    return dict(id=identity, selected_labels=list(labels), confidence=confidence)


class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.questions = [QuizQuestion('q', 'single', '按截图作答', ['C', 'A', 'B'],
            images=[QuizImage('question_q', b'\x89PNG\r\n\x1a\nTEST')], page_letters=True)]

    def solve(self, ai, **extra):
        options = dict(cross_check=True, review_model='review-vision')
        options.update(extra)
        with patch('urllib.request.build_opener', return_value=ai):
            return solve_questions(self.questions, 'https://ai.example/v1', 'text',
                'OFFLINE-KEY', vision_model='primary-vision', **options)

    def test_matching_rounds_use_original_letters_and_never_share_answers(self):
        ai = RoundAI([[choice('q', 'B', .96)], [choice('q', 'B', .91)]])
        answers = self.solve(ai)
        self.assertEqual(answers[0].selected, [2])
        self.assertEqual(answers[0].confidence, .91)
        self.assertEqual([p['model'] for p in ai.requests], ['primary-vision', 'review-vision'])
        for request in ai.requests:
            self.assertEqual([m['role'] for m in request['messages']], ['system', 'user'])
            text = json.loads(request['messages'][1]['content'][0]['text'])
            self.assertEqual(set(text), {'questions'})
        self.assertEqual(ai.requests[0]['messages'][1], ai.requests[1]['messages'][1])
        self.assertNotEqual(ai.requests[0]['messages'][0], ai.requests[1]['messages'][0])

    def test_disagreement_gets_third_blind_round_and_votes_by_id(self):
        self.questions.append(QuizQuestion('z', 'single', '第二题', ['A', 'B'], page_letters=True))
        ai = RoundAI([[choice('q', 'A'), choice('z', 'B')],
                      [choice('z', 'B'), choice('q', 'B', .93)],
                      [choice('q', 'B', .90), choice('z', 'A')]])
        answers = self.solve(ai)
        self.assertEqual([(a.id, a.selected, a.confidence) for a in answers],
                         [('q', [2], .90), ('z', [1], .95)])
        self.assertEqual(len(ai.requests), 3)
        self.assertTrue(all(p['messages'][1] == ai.requests[0]['messages'][1] for p in ai.requests))

    def test_three_different_answers_return_no_result(self):
        ai = RoundAI([[choice('q', 'A')], [choice('q', 'B')], [choice('q', 'C')]])
        with self.assertRaisesRegex(QuizAIError, '共识'):
            self.solve(ai)
        self.assertEqual(len(ai.requests), 3)

    def test_multiple_order_does_not_trigger_an_extra_round(self):
        self.questions[0].kind = 'multiple'
        ai = RoundAI([[choice('q', 'BA')], [choice('q', 'AB')]])
        self.assertEqual(set(self.solve(ai)[0].selected), {1, 2})
        self.assertEqual(len(ai.requests), 2)

    def test_fill_and_drag_are_compared_without_changing_their_answers(self):
        self.questions = [QuizQuestion('f', 'fill', '填空', [], blanks=[QuizBlank('b')]),
            QuizQuestion('d', 'drag', '连线', [],
                drag_sources=[QuizDragItem('s1', '一'), QuizDragItem('s2', '二')],
                drag_targets=[QuizDragItem('t1', '甲'), QuizDragItem('t2', '乙')])]
        drag = dict(id='d', placements={'t2': 's1', 't1': 's2'}, confidence=.97)
        def fill(value):
            return dict(id='f', filled={'b': value}, confidence=.95)
        ai = RoundAI([[fill('Are'), drag], [drag, fill('are')], [fill('are'), drag]])
        answers = self.solve(ai)
        self.assertEqual(answers[0].filled, {'b': 'are'})
        self.assertEqual(answers[1].placements, {'t1': 's2', 't2': 's1'})
        self.assertEqual(len(ai.requests), 3)

    def test_same_fill_with_different_word_sources_requires_a_vote(self):
        self.questions = [QuizQuestion('f', 'fill', '填空', [], blanks=[QuizBlank('b')],
                                      word_bank=['run', 'running'])]
        def answer(source):
            return [dict(id='f', filled={'b': 'running'}, sources={'b': source}, confidence=.95)]
        ai = RoundAI([answer(0), answer(1), answer(1)])
        self.assertEqual(self.solve(ai)[0].sources, {'b': 1})
        self.assertEqual(len(ai.requests), 3)

    def test_missing_low_confidence_or_failed_review_never_falls_back(self):
        for review in ([], [choice('q', 'B', .4)], TimeoutError('OFFLINE-KEY')):
            with self.subTest(review=type(review).__name__):
                ai = RoundAI([[choice('q', 'B')], review])
                with self.assertRaises(QuizAIError) as caught:
                    self.solve(ai)
                self.assertNotIn('OFFLINE-KEY', str(caught.exception))
                self.assertEqual(len(ai.requests), 2)

    def test_third_round_failure_does_not_pick_the_first_answer(self):
        ai = RoundAI([[choice('q', 'A')], [choice('q', 'B')], [choice('q', 'C', .4)]])
        with self.assertRaises(QuizAIError):
            self.solve(ai)
        self.assertEqual(len(ai.requests), 3)

    def test_stop_between_rounds_prevents_the_next_request(self):
        from gui_backend import Bridge, Cancelled
        bridge = Bridge()
        ai = RoundAI([[choice('q', 'B')]], after=bridge.stop)
        with self.assertRaises(Cancelled):
            self.solve(ai, check=bridge.check)
        self.assertEqual(len(ai.requests), 1)

    def test_stop_during_transcription_prevents_remaining_audio_and_solving(self):
        from gui_backend import Bridge, Cancelled
        self.questions[0].audio = [QuizAudio('a1', b'audio1', 'wav'), QuizAudio('a2', b'audio2', 'wav')]
        bridge = Bridge()
        ai = RoundAI([])
        def transcribe(*args):
            bridge.stop()
            return 'Offline transcript'
        with patch('quiz_speech.transcribe', side_effect=transcribe) as speech:
            with self.assertRaises(Cancelled):
                self.solve(ai, check=bridge.check, speech_base_url='https://speech.example/v1',
                           speech_model='speech', speech_api_key='OFFLINE-SPEECH-KEY')
        self.assertEqual(speech.call_count, 1)
        self.assertEqual(ai.requests, [])

    def test_transcripts_are_created_once_and_shared_by_all_rounds(self):
        self.questions[0].audio = [QuizAudio('a1', b'audio1', 'wav'), QuizAudio('a2', b'audio2', 'wav')]
        ai = RoundAI([[choice('q', 'A')], [choice('q', 'B')], [choice('q', 'B')]])
        with patch('quiz_speech.transcribe', side_effect=['First transcript', 'Second transcript']) as speech:
            self.solve(ai, speech_base_url='https://speech.example/v1', speech_model='speech',
                       speech_api_key='OFFLINE-SPEECH-KEY')
        self.assertEqual(speech.call_count, 2)
        for request in ai.requests:
            data = json.loads(request['messages'][1]['content'][0]['text'])
            self.assertEqual(data['questions'][0]['audio_transcripts'],
                             {'a1': 'First transcript', 'a2': 'Second transcript'})

    def test_disabled_is_one_call_and_blank_review_reuses_the_image_model(self):
        ai = RoundAI([[choice('q', 'B')]])
        self.assertEqual(self.solve(ai, cross_check=False)[0].selected, [2])
        self.assertEqual(len(ai.requests), 1)
        ai = RoundAI([[choice('q', 'B')], [choice('q', 'B')]])
        self.solve(ai, review_model='')
        self.assertEqual([p['model'] for p in ai.requests], ['primary-vision', 'primary-vision'])

