import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error


class DOMReadTests(unittest.TestCase):
    def test_read_retries_destroyed_context_then_returns_new_dom(self):
        from dom_read import retry_dom_read
        read = Mock(side_effect=[Error('Execution context was destroyed'), 'new quiz'])
        with patch('dom_read.time.sleep'):
            self.assertEqual(retry_dom_read(read)(), 'new quiz')
        self.assertEqual(read.call_count, 2)

    def test_permanent_error_is_not_retried(self):
        from dom_read import retry_dom_read
        read = Mock(side_effect=Error('SyntaxError: bad selector'))
        with self.assertRaises(Error):
            retry_dom_read(read)()
        self.assertEqual(read.call_count, 1)

    def test_loading_retry_is_bounded_and_has_clear_message(self):
        from browser import ManualRequired
        from dom_read import retry_dom_read
        read = Mock(side_effect=Error('Frame was detached'))
        with patch('dom_read.time.sleep'), self.assertRaisesRegex(ManualRequired, '加载'):
            retry_dom_read(read)()
        self.assertEqual(read.call_count, 3)

    def test_diagnostic_does_not_include_exception_url_or_message(self):
        from dom_read import safe_error_location
        try:
            raise Error('https://example.test/?token=secret')
        except Error as exc:
            message = safe_error_location(exc)
        self.assertNotIn('secret', message)
        self.assertNotIn('example.test', message)
        self.assertIn('Error', message)


if __name__ == '__main__':
    unittest.main()
