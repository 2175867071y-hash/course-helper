from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from activities import Activity, CourseActivities
from core import Store, VideoState
from main import Task, run_task
from tests.test_runner import MixedCourse, URL1, URL2


class CourseCheckpointTests(unittest.TestCase):
    def test_records_before_processing_document_or_quiz_and_survives_restart(self):
        for kind in ('document', 'quiz'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'progress.sqlite3'
                store = Store(path)
                adapter = MixedCourse([kind])
                course = CourseActivities(adapter)
                item = Activity(kind, kind, None, None)
                seen = []

                def stop_after_recognition(*args):
                    seen.append(store.resume_url('t'))
                    raise KeyboardInterrupt

                try:
                    with patch.object(course, '_detect', return_value=item), \
                            patch.object(course, '_' + kind, side_effect=stop_after_recognition):
                        outcome = run_task(adapter, Task('t', 'chaoxing', URL1),
                                           store, 0, 90, activities=course)
                    self.assertEqual(outcome, 'q')
                    self.assertEqual(seen, [URL1])
                    self.assertEqual(store.rows(), [])
                finally:
                    store.close()
                reopened = Store(path)
                try:
                    self.assertEqual(reopened.resume_url('t'), URL1)
                finally:
                    reopened.close()

    def test_mixed_course_updates_position_and_unchanged_url_does_not_write(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        video = VideoState('v', 2, 10, False, False)
        store.save('course1', URL1, video, 'observing')
        store.save_page('course1', URL2)
        changes = store._connection.total_changes
        store.save_page('course1', URL2)
        self.assertEqual(store._connection.total_changes, changes)
        store.save_page('course2', URL1)
        self.assertEqual(store.resume_url('course1'), URL2)
        store.save('course1', URL1, video, 'observing')
        self.assertEqual(store.resume_url('course1'), URL1)
        self.assertEqual(store.resume_url('course2'), URL1)
        self.assertEqual(len(store.rows()), 1)

    def test_first_activity_after_upgrade_supersedes_legacy_video_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE observations(task_id TEXT, video_key TEXT, url TEXT, '
                           'snapshot TEXT, status TEXT, updated_at TEXT, revision INTEGER, '
                           'PRIMARY KEY(task_id, video_key))')
                db.execute('INSERT INTO observations VALUES(?, ?, ?, ?, ?, ?, ?)',
                           ('t', 'v', URL1, '{}', 'video_ended', 'old', 1))
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.resume_url('t'), URL1)
                store.save_page('t', URL2)
                self.assertEqual(store.resume_url('t'), URL2)
                self.assertEqual(len(store.rows()), 1)
            finally:
                store.close()

    def test_embedded_credentials_do_not_replace_valid_resume_position(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.save_page('t', URL1)
        with self.assertRaises(ValueError):
            store.save_page('t', 'https://user:pass@chaoxing.com/lesson')
        self.assertEqual(store.resume_url('t'), URL1)
