"""GUI 测试结束后必须在创建窗口的主线程释放 Tcl 变量。"""
import gc
import tkinter as tk
import unittest
import weakref
from unittest.mock import patch

from tests import test_gui, test_parallel_gui, test_uai_platform


class GuiCleanupTests(unittest.TestCase):
    def test_gui_fixtures_release_variables_before_next_worker_starts(self):
        original_init = tk.Variable.__init__
        references = []

        def tracked_init(variable, *args, **kwargs):
            original_init(variable, *args, **kwargs)
            references.append(weakref.ref(variable))

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            for case in (
                test_gui.GuiTests('test_settings_save_without_remembering_persists_preferences_only'),
                test_parallel_gui.ParallelGuiTests(
                    'test_settings_remain_available_and_new_jobs_get_new_ai_snapshot'),
                test_uai_platform.UaiGuiTests('test_uai_start_creates_correct_request'),
            ):
                references.clear()
                result = unittest.TestResult()
                with patch.object(tk.Variable, '__init__', tracked_init):
                    case.run(result)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                self.assertTrue(references)
                self.assertEqual(sum(reference() is not None for reference in references), 0,
                                 f'{case.id()} 将 Tcl 变量遗留给了后续线程 GC')
        finally:
            # 回归失败时也在主线程回收，不能把未释放窗口带入后续线程测试。
            gc.collect()
            if was_enabled:
                gc.enable()


if __name__ == '__main__':
    unittest.main()
