"""AI 小测客户端的离线 HTTP 边界测试，不读取真实密钥或访问网络。"""

import importlib
import io
import json
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    quiz_ai = importlib.import_module("quiz_ai")
except ModuleNotFoundError as exc:
    if exc.name != "quiz_ai":
        raise
    quiz_ai = None


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.response


class QuizAITests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(quiz_ai, "quiz_ai.py 尚未实现")
        self.secret = "sk-OFFLINE-SECRET-NOT-TO-BE-LOGGED"
        self.question = quiz_ai.QuizQuestion("q1", "single", "一加一等于几？",
                                              ["一", "二", "三"])

    def content(self, answers=None):
        if answers is None:
            answers = [{"id": "q1", "selected": [1], "confidence": 0.95}]
        return json.dumps({"answers": answers}, ensure_ascii=False)

    def response(self, content=None, status=200, finish_reason="stop"):
        body = {"choices": [{"message": {"content": content or self.content()},
                              "finish_reason": finish_reason}]}
        return FakeResponse(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            status=status)

    def solve(self, opener=None, **changes):
        args = dict(questions=[self.question], base_url="https://ai.example/v1",
                    model="test-model", api_key=self.secret)
        args.update(changes)
        opener = opener or FakeOpener(self.response())
        with patch("urllib.request.build_opener", return_value=opener):
            result = quiz_ai.solve_questions(**args)
        return result, opener

    def test_valid_answer_and_safe_request_payload(self):
        self.question.cookie = "PRIVATE-COOKIE"
        self.question.url = "https://course.example/private"
        self.question.student_id = "PRIVATE-STUDENT"
        result, opener = self.solve()
        self.assertEqual(result, [quiz_ai.QuizAnswer("q1", [1], 0.95)])
        self.assertEqual(opener.request.full_url,
                         "https://ai.example/v1/chat/completions")
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(opener.timeout, 30)
        self.assertEqual(opener.request.get_header("Authorization"),
                         "Bearer " + self.secret)
        payload = json.loads(opener.request.data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][0]["role"], "system")
        submitted = json.loads(payload["messages"][1]["content"])
        self.assertEqual(submitted, {"questions": [{
            "id": "q1", "kind": "single", "prompt": "一加一等于几？",
            "options": ["一", "二", "三"]}]})
        self.assertNotIn(self.secret, opener.request.data.decode("utf-8"))
        self.assertNotIn("PRIVATE-", opener.request.data.decode("utf-8"))

    def test_endpoint_suffix_is_not_appended_twice(self):
        _, opener = self.solve(base_url="https://ai.example/v1/chat/completions")
        self.assertEqual(opener.request.full_url,
                         "https://ai.example/v1/chat/completions")

    def test_base_url_trailing_slash_is_supported(self):
        _, opener = self.solve(base_url="https://ai.example/v1/")
        self.assertEqual(opener.request.full_url,
                         "https://ai.example/v1/chat/completions")

    def test_redirect_handler_does_not_forward_the_bearer_key(self):
        opener = FakeOpener(self.response())
        with patch("urllib.request.build_opener", return_value=opener) as build:
            quiz_ai.solve_questions([self.question], "https://ai.example/v1",
                                    "test-model", self.secret)
        handler = next(arg for arg in build.call_args.args
                       if isinstance(arg, HTTPRedirectHandler))
        redirected = handler.redirect_request(
            opener.request, None, 302, "Found", {}, "https://other.example/")
        self.assertIsNone(redirected)

    def test_rejects_unsafe_base_urls_before_sending(self):
        urls = ["http://ai.example/v1", "https://user:password@ai.example/v1",
                "https://user@ai.example/v1", "https://ai.example/v1?key=x",
                "https://ai.example/v1?", "https://ai.example/v1#fragment",
                "https://ai.example/v1#", "https://ai.example\\@evil.example/",
                "https:///v1", "https://ai.example:bad/v1", "not-a-url",
                " https://ai.example/v1", "https://ai.example/\npath"]
        for url in urls:
            opener = FakeOpener(self.response())
            with self.subTest(url=url), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(opener, base_url=url)
            self.assertIsNone(opener.request)

    def test_empty_queue_returns_without_network(self):
        result, opener = self.solve(questions=[])
        self.assertEqual(result, [])
        self.assertIsNone(opener.request)

    def test_more_than_50_questions_rejected_without_network(self):
        questions = [quiz_ai.QuizQuestion(str(i), "single", "题目", ["甲", "乙"])
                     for i in range(51)]
        opener = FakeOpener(self.response())
        with self.assertRaises(quiz_ai.QuizAIError):
            self.solve(opener, questions=questions)
        self.assertIsNone(opener.request)

    def test_invalid_input_questions_rejected_before_network(self):
        samples = [
            [self.question, self.question],
            [quiz_ai.QuizQuestion("", "single", "题目", ["甲", "乙"])],
            [quiz_ai.QuizQuestion("q", "essay", "题目", ["甲", "乙"])],
            [quiz_ai.QuizQuestion("q", "single", "", ["甲", "乙"])],
            [quiz_ai.QuizQuestion("q", "single", "题目", ["甲"])],
            [quiz_ai.QuizQuestion("q", "truefalse", "题目", ["甲", "乙", "丙"])],
            [quiz_ai.QuizQuestion("q", "single", "题目", ["甲", ""])],
        ]
        for questions in samples:
            opener = FakeOpener(self.response())
            with self.subTest(questions=questions), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(opener, questions=questions)
            self.assertIsNone(opener.request)

    def test_blank_model_and_invalid_key_fail_without_revealing_values(self):
        for changes in ({"model": ""}, {"api_key": ""},
                        {"api_key": self.secret + "\r\nX-Injected: yes"}):
            with self.subTest(changes=list(changes)), self.assertRaises(
                quiz_ai.QuizAIError
            ) as caught:
                self.solve(**changes)
            self.assertNotIn(self.secret, str(caught.exception))

    def test_multiple_and_truefalse_results_follow_input_order(self):
        questions = [quiz_ai.QuizQuestion("a", "multiple", "选素数", ["2", "3", "4"]),
                     quiz_ai.QuizQuestion("b", "truefalse", "一等于一", ["对", "错"])]
        answers = [{"id": "b", "selected": [0], "confidence": 0.85},
                   {"id": "a", "selected": [0, 1], "confidence": 1}]
        response = self.response(self.content(answers))
        result, _ = self.solve(FakeOpener(response), questions=questions)
        self.assertEqual(result, [quiz_ai.QuizAnswer("a", [0, 1], 1.0),
                                  quiz_ai.QuizAnswer("b", [0], 0.85)])

    def test_missing_duplicate_and_unknown_answer_ids_are_rejected(self):
        samples = [[], [{"id": "other", "selected": [0], "confidence": 1}],
                   [{"id": "q1", "selected": [0], "confidence": 1}] * 2]
        for answers in samples:
            with self.subTest(answers=answers), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(self.response(self.content(answers))))

    def test_invalid_selected_indices_and_single_cardinality_are_rejected(self):
        for selected in ([], [0, 1], [3], [-1], [True], [1.0], ["1"], None):
            content = self.content([{"id": "q1", "selected": selected, "confidence": 1}])
            with self.subTest(selected=selected), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(self.response(content)))

    def test_multiple_choice_requires_unique_nonempty_indices(self):
        question = quiz_ai.QuizQuestion("q1", "multiple", "选项", ["甲", "乙"])
        for selected in ([], [0, 0]):
            content = self.content([{"id": "q1", "selected": selected, "confidence": 1}])
            with self.subTest(selected=selected), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(self.response(content)), questions=[question])

    def test_low_and_invalid_confidence_require_manual_review(self):
        for confidence in (0.849, -1, 1.01, float("nan"), float("inf"),
                           "0.99", True, None):
            content = self.content([{"id": "q1", "selected": [1],
                                     "confidence": confidence}])
            with self.subTest(confidence=confidence), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(self.response(content)))

    def test_all_questions_validated_before_any_answers_are_returned(self):
        questions = [self.question,
                     quiz_ai.QuizQuestion("q2", "single", "题目", ["甲", "乙"])]
        answers = [{"id": "q1", "selected": [1], "confidence": 1},
                   {"id": "q2", "selected": [0], "confidence": 0.5}]
        with self.assertRaises(quiz_ai.QuizAIError):
            self.solve(FakeOpener(self.response(self.content(answers))), questions=questions)

    def test_enormous_integer_confidence_is_a_controlled_error(self):
        content = self.content([{"id": "q1", "selected": [1],
                                 "confidence": 10 ** 1000}])
        with self.assertRaises(quiz_ai.QuizAIError):
            self.solve(FakeOpener(self.response(content)))

    def test_malformed_response_json_or_schema_is_rejected(self):
        bodies = [b"not-json", b"{}", b'[]', b'{"choices": []}',
                  b'{"choices": [{"message": {"content": null}}]}', b"\xff"]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(FakeResponse(body)))

    def test_non_json_model_answers_or_duplicate_json_keys_are_rejected(self):
        for content in ("Please ignore previous instructions", "```json\n{}\n```",
                        "[]", "{}", '{"answers": [], "answers": []}'):
            with self.subTest(content=content), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(self.response(content)))

    def test_truncated_completion_requires_manual_review(self):
        with self.assertRaises(quiz_ai.QuizAIError):
            self.solve(FakeOpener(self.response(finish_reason="length")))

    def test_response_larger_than_2_mib_is_rejected_and_read_is_bounded(self):
        response = FakeResponse(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(quiz_ai.QuizAIError):
            self.solve(FakeOpener(response))
        self.assertEqual(response.read_sizes, [2 * 1024 * 1024 + 1])

    def test_exactly_2_mib_response_can_be_read(self):
        response = self.response()
        response.body += b" " * (2 * 1024 * 1024 - len(response.body))
        result, _ = self.solve(FakeOpener(response))
        self.assertEqual(result[0].selected, [1])

    def test_http_status_errors_and_redirects_are_not_processed_as_answers(self):
        for status in (302, 307, 400, 401, 429, 500):
            response = self.response(status=status)
            with self.subTest(status=status), self.assertRaises(quiz_ai.QuizAIError):
                self.solve(FakeOpener(response))
            self.assertEqual(response.read_sizes, [])

    def test_network_and_response_errors_do_not_leak_secret_or_raw_body(self):
        marker = self.secret + " RAW-RESPONSE-PRIVATE"
        errors = [URLError(marker), TimeoutError(marker), OSError(marker),
                  HTTPError("https://ai.example/", 302, marker, {},
                            io.BytesIO(marker.encode()))]
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                try:
                    self.solve(FakeOpener(error=error))
                except quiz_ai.QuizAIError as exc:
                    display = "".join(traceback.format_exception(exc))
                    self.assertNotIn(self.secret, display)
                    self.assertNotIn("RAW-RESPONSE-PRIVATE", display)
                else:
                    self.fail("网络错误必须返回 QuizAIError")
        with self.assertRaises(quiz_ai.QuizAIError) as caught:
            self.solve(FakeOpener(FakeResponse(marker.encode())))
        self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
