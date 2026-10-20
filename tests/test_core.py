"""核心状态机与本地记录的行为测试；无需浏览器或网络。"""

import importlib
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    core = importlib.import_module("core")
except ModuleNotFoundError as exc:
    if exc.name != "core":
        raise
    core = None


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(core, "core.py 尚未实现")

    def state(self, current=0, duration=100, **changes):
        values = dict(key="v1", current=current, duration=duration,
                      paused=False, ended=False, ready=4, error=0)
        values.update(changes)
        return core.VideoState(**values)


class MonitorTests(CoreTestCase):
    def test_initial_pause_starts_only_once(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(paused=True), 0), "start")
        self.assertEqual(monitor.step(self.state(paused=True), 1), "manual")
        self.assertEqual(monitor.step(self.state(paused=True), 2), "manual")

    def test_new_video_gets_its_own_initial_start(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(paused=True), 0), "start")
        self.assertEqual(monitor.step(self.state(1), 1), "wait")
        self.assertEqual(monitor.step(self.state(key="v2", paused=True), 2),
                         "start")

    def test_running_video_is_not_started_again(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(25), 0), "wait")
        self.assertEqual(monitor.step(self.state(26), 1), "wait")

    def test_natural_end_emits_once_after_observed_progress(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(98), 0), "wait")
        self.assertEqual(monitor.step(self.state(99), 1), "wait")
        final = self.state(100, paused=True, ended=True)
        self.assertEqual(monitor.step(final, 2), "finished")
        self.assertEqual(monitor.step(final, 3), "wait")

    def test_progress_observed_in_final_sample_is_sufficient(self):
        monitor = core.Monitor()
        monitor.step(self.state(99), 0)
        self.assertEqual(monitor.step(self.state(100, ended=True), 1),
                         "finished")

    def test_first_observation_already_ended_requires_person(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(100, ended=True), 0),
                         "manual")
        self.assertTrue(monitor.reason)

    def test_initial_end_with_missing_metadata_also_requires_person(self):
        state = self.state(duration=float("nan"), ready=0, ended=True)
        self.assertEqual(core.Monitor().step(state, 0), "manual")

    def test_ended_without_progress_does_not_finish(self):
        monitor = core.Monitor()
        monitor.step(self.state(100), 0)
        self.assertEqual(monitor.step(self.state(100, ended=True), 1),
                         "manual")

    def test_false_end_far_from_duration_requires_person(self):
        monitor = core.Monitor()
        monitor.step(self.state(20), 0)
        self.assertEqual(monitor.step(self.state(21, ended=True), 1),
                         "manual")

    def test_mid_play_pause_locks_even_on_new_video(self):
        monitor = core.Monitor()
        monitor.step(self.state(), 0)
        monitor.step(self.state(1), 1)
        self.assertEqual(monitor.step(self.state(1, paused=True), 2),
                         "manual")
        reason = monitor.reason
        self.assertEqual(monitor.step(self.state(key="v2"), 3), "manual")
        self.assertEqual(monitor.reason, reason)

    def test_buffering_pause_waits_for_recovery(self):
        monitor = core.Monitor(stall_seconds=10)
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(paused=True, ready=2), 1),
                         "wait")
        self.assertEqual(monitor.step(self.state(1), 2), "wait")

    def test_initial_metadata_only_allows_single_start(self):
        for ready in (1, 2):
            with self.subTest(ready=ready):
                monitor = core.Monitor()
                state = self.state(paused=True, ready=ready)
                self.assertEqual(monitor.step(state, 0), "start")
                self.assertEqual(monitor.step(state, 1), "wait")
                self.assertEqual(monitor.step(self.state(paused=True), 2), "manual")

    def test_buffering_timeout_locks(self):
        monitor = core.Monitor(stall_seconds=10)
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(ready=2), 10), "manual")

    def test_stalled_running_video_times_out(self):
        monitor = core.Monitor(stall_seconds=10)
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(), 9), "wait")
        self.assertEqual(monitor.step(self.state(), 10), "manual")

    def test_media_error_and_blocker_latch(self):
        for state, blocked in ((self.state(error=3), False),
                               (self.state(), True)):
            with self.subTest(blocked=blocked):
                monitor = core.Monitor()
                self.assertEqual(monitor.step(state, 0, blocked=blocked),
                                 "manual")
                self.assertEqual(monitor.step(self.state(key="v2"), 1),
                                 "manual")

    def test_invalid_media_numbers_require_person(self):
        samples = [self.state(current=float("nan")),
                   self.state(current=float("inf")),
                   self.state(current=-1), self.state(current=105),
                   self.state(duration=float("nan")),
                   self.state(duration=float("inf")),
                   self.state(duration=-1), self.state(duration=0)]
        for state in samples:
            with self.subTest(state=state):
                self.assertEqual(core.Monitor().step(state, 0), "manual")

    def test_pending_metadata_starts_once_then_observes_playback(self):
        monitor = core.Monitor()
        missing = self.state(duration=float("nan"), ready=0, paused=True)
        self.assertEqual(monitor.step(missing, 0), "start")
        self.assertEqual(monitor.step(missing, 1), "wait")
        self.assertEqual(monitor.step(self.state(duration=2), 2), "wait")
        self.assertEqual(monitor.step(self.state(1, duration=2), 3), "wait")
        self.assertEqual(monitor.step(self.state(2, duration=2, ended=True), 4), "finished")

    def test_metadata_wait_has_a_timeout(self):
        monitor = core.Monitor(stall_seconds=10)
        missing = self.state(duration=0, ready=0, paused=True)
        self.assertEqual(monitor.step(missing, 0), "start")
        self.assertEqual(monitor.step(missing, 1), "wait")
        self.assertEqual(monitor.step(missing, 10), "manual")

    def test_playing_without_metadata_is_not_started_again(self):
        monitor = core.Monitor()
        missing = self.state(duration=0, ready=0, paused=False)
        self.assertEqual(monitor.step(missing, 0), "wait")
        self.assertEqual(monitor.step(self.state(duration=0, ready=0, paused=True), 1), "wait")

    def test_cold_video_with_blocker_error_or_ended_cannot_start(self):
        for changes, blocked in (({}, True), ({'error': 3}, False), ({'ended': True}, False)):
            with self.subTest(changes=changes, blocked=blocked):
                state = self.state(duration=0, ready=0, paused=True, **changes)
                self.assertEqual(core.Monitor().step(state, 0, blocked=blocked), "manual")

    def test_backward_seek_requires_person(self):
        monitor = core.Monitor()
        monitor.step(self.state(20), 0)
        self.assertEqual(monitor.step(self.state(10), 1), "manual")

    def test_forward_jump_is_not_natural_completion(self):
        monitor = core.Monitor()
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(100, ended=True), 1),
                         "manual")

    def test_paused_buffering_samples_do_not_prove_normal_playback(self):
        monitor = core.Monitor()
        self.assertEqual(monitor.step(self.state(9, 10, paused=True, ready=2), 0),
                         "start")
        self.assertEqual(monitor.step(
            self.state(10, 10, paused=True, ended=True, ready=2), 1), "manual")

    def test_small_repeated_jumps_cannot_accumulate_into_fast_completion(self):
        monitor = core.Monitor()
        actions = [monitor.step(self.state(2 * t, 10, ended=(t == 5)), t)
                   for t in range(6)]
        self.assertNotIn("finished", actions)
        self.assertEqual(actions[-1], "manual")

    def test_duration_change_requires_person(self):
        monitor = core.Monitor()
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(1, duration=200), 1),
                         "manual")

    def test_clock_must_be_finite_and_monotonic(self):
        for now in (float("inf"), float("nan")):
            self.assertEqual(core.Monitor().step(self.state(), now), "manual")
        monitor = core.Monitor()
        monitor.step(self.state(), 5)
        self.assertEqual(monitor.step(self.state(1), 4), "manual")

    def test_monitoring_gap_does_not_imply_continuous_playback(self):
        monitor = core.Monitor(stall_seconds=10)
        monitor.step(self.state(), 0)
        self.assertEqual(monitor.step(self.state(100, ended=True), 100),
                         "manual")

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                core.Monitor(stall_seconds=timeout)


class UrlTests(CoreTestCase):
    def test_accepts_exact_host_and_subdomain(self):
        cases = [("chaoxing", "https://chaoxing.com/course?x=1#part"),
                 ("chaoxing", "https://mooc1.chaoxing.com:443/course"),
                 ("mooc", "https://www.icourse163.org/learn/example"),
                 ("mooc", "https://ICOURSE163.ORG/course")]
        for platform, url in cases:
            with self.subTest(url=url):
                self.assertEqual(core.validate_url(url, platform), url)

    def test_rejects_untrusted_urls(self):
        urls = ["http://chaoxing.com/course", "https://evilchaoxing.com/",
                "https://chaoxing.com.evil.example/", "https://evil.example/",
                "https://user:pass@chaoxing.com/", "https://user@chaoxing.com/",
                "https://chaoxing.com:444/", "https://chaoxing.com:bad/",
                "https://chaoxing.com./", "https://chaoxing.com\\@evil.test/",
                "https://chaoxing.com/\nsecret", " https://chaoxing.com/",
                "https://.chaoxing.com/", "https://a..chaoxing.com/",
                "file:///chaoxing.com", "//chaoxing.com/", ""]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                core.validate_url(url, "chaoxing")

    def test_platform_mismatch_and_unknown_platform_rejected(self):
        for platform in ("mooc", "unknown"):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                core.validate_url("https://chaoxing.com/", platform)


class StoreTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "progress.sqlite3"
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)

    def test_saves_observation_without_inventing_completion(self):
        self.store.save("t1", "https://chaoxing.com/c1", self.state(37.25),
                        "playing")
        row = self.store.rows()[0]
        self.assertEqual(row["task_id"], "t1")
        self.assertEqual(row["video_key"], "v1")
        self.assertEqual(row["current"], 37.25)
        self.assertEqual(row["duration"], 100)
        self.assertEqual(row["status"], "playing")
        self.assertFalse(row["ended"])

    def test_keeps_latest_per_task_video_and_latest_resume_url(self):
        self.store.save("t1", "https://chaoxing.com/c1", self.state(1), "playing")
        self.store.save("t1", "https://chaoxing.com/c2", self.state(key="v2"),
                        "playing")
        self.store.save("t1", "https://chaoxing.com/c1", self.state(5), "paused")
        rows = self.store.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(row for row in rows if row["video_key"] == "v1")
                         ["current"], 5)
        self.assertEqual(self.store.resume_url("t1"), "https://chaoxing.com/c1")

    def test_tasks_are_isolated_and_unknown_task_has_no_resume(self):
        self.store.save("t1", "https://chaoxing.com/c1", self.state(2), "playing")
        self.store.save("t2", "https://chaoxing.com/c2", self.state(4), "playing")
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.store.resume_url("t1"), "https://chaoxing.com/c1")
        self.assertIsNone(self.store.resume_url("missing"))

    def test_records_survive_reopening_and_close_is_idempotent(self):
        self.store.save("t1", "https://chaoxing.com/c1", self.state(15), "manual")
        self.store.close()
        self.store.close()
        reopened = core.Store(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.rows()[0]["current"], 15)
        self.assertEqual(reopened.rows()[0]["status"], "manual")

    def test_nonfinite_observations_are_retained_for_diagnosis(self):
        self.store.save("t1", "https://chaoxing.com/c1",
                        self.state(duration=float("nan"), ready=0), "manual")
        row = self.store.rows()[0]
        self.assertTrue(math.isnan(row["duration"]))
        self.assertEqual(row["ready"], 0)

    def test_does_not_record_embedded_credentials(self):
        with self.assertRaises(ValueError):
            self.store.save("t1", "https://user:pass@chaoxing.com/c1",
                            self.state(), "playing")
        self.assertEqual(self.store.rows(), [])


if __name__ == "__main__":
    unittest.main()
