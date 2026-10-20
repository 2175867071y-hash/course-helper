"""多空与公共材料的离线接口测试。"""
import json
import unittest
from unittest.mock import patch

import quiz_ai as ai
from test_quiz_ai import FakeOpener, FakeResponse


class ExtendedAITests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ai, 'QuizBlank'), '尚未提供逐空题目结构')

    def question(self, **changes):
        args = dict(id='q', kind='fill', prompt='He [[a]] the map [[b]].', options=[],
                    blanks=[ai.QuizBlank('a'), ai.QuizBlank('b')])
        args.update(changes)
        return ai.QuizQuestion(**args)

    def solve(self, questions, answers):
        body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'answers': answers})}}]}).encode()
        opener = FakeOpener(FakeResponse(body))
        with patch('urllib.request.build_opener', return_value=opener):
            result = ai.solve_questions(questions, 'https://ai.example/v1', 'offline', 'key')
        return result, opener

    def answer(self, **changes):
        args = dict(id='q', filled={'b': 'out', 'a': 'laid'}, confidence=.99)
        args.update(changes)
        return args

    def test_two_blanks_keep_ids_and_include_shared_context(self):
        question = self.question(context='Use the past tense.', word_bank=['lay … out'])
        question.cookie = 'PRIVATE-COOKIE'
        result, opener = self.solve([question], [self.answer(sources={'a': 0, 'b': 0})])
        self.assertEqual(result[0].filled, {'a': 'laid', 'b': 'out'})
        self.assertEqual(result[0].sources, {'a': 0, 'b': 0})
        sent = json.loads(json.loads(opener.request.data)['messages'][1]['content'])['questions'][0]
        self.assertEqual(sent['context'], 'Use the past tense.')
        self.assertEqual(sent['word_bank'], ['lay … out'])
        self.assertEqual([x['id'] for x in sent['blanks']], ['a', 'b'])
        self.assertNotIn('PRIVATE-COOKIE', str(sent))

    def test_missing_extra_or_empty_blank_answers_fail(self):
        for filled in ({'a': 'laid'}, {'a': 'laid', 'b': 'out', 'c': 'extra'},
                       {'a': ' ', 'b': 'out'}, {'a': 2, 'b': 'out'},
                       {'a': 'laid\x00', 'b': 'out'}):
            with self.subTest(filled=filled), self.assertRaises(ai.QuizAIError):
                self.solve([self.question()], [self.answer(filled=filled)])

    def test_dropdown_and_maxlength_constraints(self):
        q = self.question(blanks=[ai.QuizBlank('a', ['laid', 'lays']), ai.QuizBlank('b', max_length=3)])
        result, _ = self.solve([q], [self.answer()])
        self.assertEqual(result[0].filled['a'], 'laid')
        for filled in ({'a': 'unknown', 'b': 'out'}, {'a': 'laid', 'b': 'outside'}):
            with self.subTest(filled=filled), self.assertRaises(ai.QuizAIError):
                self.solve([q], [self.answer(filled=filled)])

    def test_word_bank_sources_must_cover_each_blank(self):
        q = self.question(word_bank=['lay … out'])
        for sources in (None, {}, {'a': 0}, {'a': 0, 'b': 1}, {'a': True, 'b': 0}):
            a = self.answer()
            if sources is not None:
                a['sources'] = sources
            with self.subTest(sources=sources), self.assertRaises(ai.QuizAIError):
                self.solve([q], [a])

    def test_once_rule_allows_split_phrase_but_not_reuse_across_questions(self):
        q = self.question(word_bank=['lay … out'], bank_reuse='once')
        self.solve([q], [self.answer(sources={'a': 0, 'b': 0})])
        q2 = self.question(id='q2', word_bank=['lay … out'], bank_reuse='once')
        answers = [self.answer(sources={'a': 0, 'b': 0}),
                   self.answer(id='q2', sources={'a': 0, 'b': 0})]
        with self.assertRaises(ai.QuizAIError):
            self.solve([q, q2], answers)
        q.bank_reuse = q2.bank_reuse = 'unspecified'
        self.assertEqual(len(self.solve([q, q2], answers)[0]), 2)

    def test_mixed_choice_and_fill_preserve_question_order(self):
        q = ai.QuizQuestion('s', 'single', 'Pick two', ['Two', 'Three'])
        result, _ = self.solve([q, self.question()], [self.answer(),
            {'id': 's', 'selected': [0], 'confidence': .99}])
        self.assertEqual([a.id for a in result], ['s', 'q'])
        self.assertEqual(result[0].selected, [0])

    def test_invalid_blank_definitions_and_oversized_material_rejected_before_network(self):
        cases = [self.question(blanks=[]), self.question(blanks=[ai.QuizBlank('a'), ai.QuizBlank('a')]),
                 self.question(context='x' * 50001), self.question(bank_reuse='guess'),
                 self.question(blanks=[ai.QuizBlank('a', max_length=-1)]),
                 self.question(blanks=[ai.QuizBlank('a', ['same', 'same'])])]
        for q in cases:
            with self.subTest(q=q), patch('urllib.request.build_opener') as opener:
                with self.assertRaises(ai.QuizAIError):
                    ai.solve_questions([q], 'https://ai.example', 'offline', 'key')
                opener.assert_not_called()

    def test_text_answer_cannot_smuggle_action_fields(self):
        with self.assertRaises(ai.QuizAIError):
            self.solve([self.question()], [self.answer(javascript='alert(1)')])

    def test_missing_id_is_a_controlled_error(self):
        answer = self.answer()
        del answer['id']
        with self.assertRaises(ai.QuizAIError):
            self.solve([self.question()], [answer])

    def test_invalid_unicode_never_reaches_transport_or_browser(self):
        for changes in ({'context': '\ud800'}, {'prompt': '\ud800'}, {'id': '\ud800'}):
            with self.subTest(changes=changes), self.assertRaises(ai.QuizAIError):
                self.solve([self.question(**changes)], [self.answer()])
        with self.assertRaises(ai.QuizAIError):
            self.solve([self.question()], [self.answer(filled={'a': '\ud800', 'b': 'out'})])

    def test_html_maxlength_uses_utf16_units(self):
        q = self.question(blanks=[ai.QuizBlank('a', max_length=1), ai.QuizBlank('b')])
        with self.assertRaises(ai.QuizAIError):
            self.solve([q], [self.answer(filled={'a': '😀', 'b': 'out'})])

    def test_large_combined_material_is_rejected_without_a_request(self):
        questions = [self.question(id=str(i), context='a' * 49000) for i in range(7)]
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ai.QuizAIError, '300 KB'):
                ai.solve_questions(questions, 'https://ai.example', 'offline', 'key')
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
