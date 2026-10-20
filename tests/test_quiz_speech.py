"""转写接口的路由、限额及错误保密；所有响应为本地替身。"""
import unittest
from unittest.mock import patch
from urllib.error import URLError
from quiz_ai import QuizAudio, QuizAIError
from quiz_speech import transcribe
from test_quiz_ai import FakeOpener, FakeResponse


class SpeechTests(unittest.TestCase):
    def call(self, base='https://speech.example/v1'):
        return transcribe(QuizAudio('a', b'RIFFsynthetic', 'wav'), base, 'speech-model', 'SYNTHETIC-KEY')

    def test_base_and_full_endpoint_use_same_transcription_path(self):
        for base in ('https://speech.example/v1', 'https://speech.example/v1/',
                     'https://speech.example/v1/audio/transcriptions'):
            opener = FakeOpener(FakeResponse(b'{"text":"hello"}'))
            with patch('urllib.request.build_opener', return_value=opener):
                self.assertEqual(self.call(base), 'hello')
            self.assertEqual(opener.request.full_url, 'https://speech.example/v1/audio/transcriptions')
            self.assertIn(b'name="file"; filename="listening.wav"', opener.request.data)
            self.assertEqual(opener.timeout, 30)

    def test_invalid_endpoint_never_creates_request(self):
        for base in ('http://speech.example', 'https://speech.example/v1?key=secret',
                     'https://name:password@speech.example'):
            with patch('urllib.request.build_opener') as network:
                with self.assertRaises(QuizAIError):
                    self.call(base)
            network.assert_not_called()

    def test_rejects_empty_invalid_and_oversized_transcripts(self):
        for body in (b'{"text":""}', b'[]', b'not json', b'x' * (1024 * 1024 + 1)):
            with patch('urllib.request.build_opener', return_value=FakeOpener(FakeResponse(body))):
                with self.assertRaises(QuizAIError):
                    self.call()

    def test_network_error_does_not_expose_secret(self):
        opener = FakeOpener(error=URLError('SYNTHETIC-KEY body SECRET-AUDIO'))
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(QuizAIError) as error:
                self.call()
        self.assertNotIn('SYNTHETIC-KEY', str(error.exception))
        self.assertNotIn('SECRET-AUDIO', str(error.exception))

    def test_redirect_is_not_followed_with_credentials(self):
        opener = FakeOpener(FakeResponse(b'{"text":"hello"}', 302))
        with patch('urllib.request.build_opener', return_value=opener) as builder:
            with self.assertRaises(QuizAIError):
                self.call()
        handler = builder.call_args.args[0]
        self.assertIsNone(handler.redirect_request(opener.request, None, 302, '', {}, 'https://other.example'))


if __name__ == '__main__':
    unittest.main()
