"""Check launcher polling without live backends, tokens, or clipboard access."""
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import access_window as ui


class Root:
    def __init__(self):
        self.owner = threading.get_ident()
        self.callbacks = {}
        self.serial = 0
        self.now = 0
        self.destroyed = False

    def check_thread(self):
        assert threading.get_ident() == self.owner, 'Worker called Tk'
        assert not self.destroyed, 'Callback used a destroyed window'

    def bind(self, event, callback, add=None):
        self.check_thread()
        assert event == '<Destroy>' and add == '+'
        self.on_destroy = callback

    def after(self, delay, callback):
        self.check_thread()
        self.serial += 1
        self.callbacks[self.serial] = (self.now + delay, callback)
        return self.serial

    def after_cancel(self, timer):
        self.check_thread()
        self.callbacks.pop(timer, None)

    def run_next(self):
        self.check_thread()
        timer = min(self.callbacks, key=lambda key: self.callbacks[key][0])
        self.now, callback = self.callbacks.pop(timer)
        callback()

    def destroy(self):
        self.check_thread()
        self.on_destroy(SimpleNamespace(widget=self))
        self.destroyed = True


class Status:
    def __init__(self, root):
        self.root = root
        self.value = ''

    def set(self, value):
        self.root.check_thread()
        self.value = value


class Button:
    def __init__(self, root):
        self.root = root
        self.enabled = False

    def state(self, flags):
        self.root.check_thread()
        self.enabled = flags == ['!disabled']


def arguments():
    return SimpleNamespace(
        status_urls=['https://loopback.invalid/status', 'https://fallback.invalid/status'],
        shutdown_urls=['https://loopback.invalid/shutdown'],
        shutdown_token='fixture-only', instance_id='fixture-instance',
    )


class LauncherPollingTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.status = Status(self.root)
        self.buttons = [Button(self.root), Button(self.root)]
        self.shutdown = Button(self.root)
        self.state = {'name': ui.UI_STATE_STARTING, 'offline_polls': 0}
        self.args = arguments()
        self.poller = ui.LauncherStatusPoller(
            self.root, self.args, self.status, self.state, self.buttons, self.shutdown,
        )
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads = []
        real_thread = threading.Thread

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self.threads.append(thread)
            return thread

        self.thread_patch = patch.object(ui.threading, 'Thread', side_effect=make_thread)
        self.thread_patch.start()

    def tearDown(self):
        self.release.set()
        if not self.root.destroyed:
            self.root.destroy()
        self.join_workers()
        self.thread_patch.stop()

    def join_workers(self):
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), 'Fixture worker did not finish')
            self.assertTrue(thread.daemon)

    def slow_reply(self, urls, token, instance_id):
        self.assertNotEqual(threading.get_ident(), self.root.owner)
        self.assertEqual((urls, token, instance_id),
                         (self.args.status_urls, self.args.shutdown_token, self.args.instance_id))
        self.entered.set()
        self.release.wait(timeout=3)
        return {'status': 'ok', 'instance_id': instance_id}, None

    def begin_slow_poll(self):
        self.poller.start()
        self.root.run_next()
        self.assertTrue(self.entered.wait(timeout=3))

    def complete_poll(self):
        self.root.run_next()
        self.join_workers()
        self.root.run_next()

    def test_slow_poll_keeps_ui_running_and_cannot_overlap(self):
        with patch.object(ui, 'fetch_launcher_json_any', side_effect=self.slow_reply) as fetch:
            self.begin_slow_poll()
            beats = []
            self.root.after(0, lambda: beats.append('responsive'))
            for _ in range(5):
                self.poller.start()
                self.root.run_next()
            self.assertEqual(beats, ['responsive'])
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.state['name'], ui.UI_STATE_STARTING)
            self.assertFalse(any(button.enabled for button in self.buttons))
            self.release.set()
            self.join_workers()
            self.root.run_next()
        self.assertEqual(self.state['name'], ui.UI_STATE_CURRENT)
        self.assertTrue(all(button.enabled for button in self.buttons))
        self.assertTrue(self.shutdown.enabled)
        self.assertEqual(len(self.root.callbacks), 1)

    def test_close_drops_pending_reply_without_waiting_or_touching_tk(self):
        with patch.object(ui, 'fetch_launcher_json_any', side_effect=self.slow_reply):
            self.begin_slow_poll()
            self.root.destroy()
            self.poller.start()
            self.assertFalse(self.release.is_set())
            self.assertFalse(self.root.callbacks)
            self.release.set()
            self.join_workers()
        self.assertEqual(self.status.value, '')
        self.assertEqual(self.state['name'], ui.UI_STATE_STARTING)

    def test_child_destroy_does_not_stop_polling_and_early_close_cancels_start(self):
        self.poller.start()
        self.root.on_destroy(SimpleNamespace(widget=object()))
        self.assertFalse(self.poller.closed)
        self.assertEqual(len(self.root.callbacks), 1)
        self.root.destroy()
        self.assertFalse(self.root.callbacks)
        self.assertFalse(self.threads)

    def test_transient_offline_recovers_and_repeated_offline_still_closes(self):
        offline = (None, ui.make_launcher_error(ui.ERROR_UNREACHABLE, 'Fixture timeout'))
        replies = [offline, ({'status': 'ok'}, None)] + [offline] * ui.LAUNCHER_OFFLINE_POLL_LIMIT
        with patch.object(ui, 'fetch_launcher_json_any', side_effect=replies):
            self.poller.start()
            self.complete_poll()
            self.assertEqual(self.state['name'], ui.UI_STATE_OFFLINE)
            self.assertFalse(self.shutdown.enabled)
            self.complete_poll()
            self.assertEqual(self.state, {'name': ui.UI_STATE_CURRENT, 'offline_polls': 0})
            for _ in range(ui.LAUNCHER_OFFLINE_POLL_LIMIT):
                self.complete_poll()
        self.assertTrue(self.root.destroyed)
        self.assertFalse(self.root.callbacks)

    def test_unauthorized_reply_stops_without_fallback(self):
        denied = ui.make_launcher_error(ui.ERROR_UNAUTHORIZED, 'Fixture rejection')
        with patch.object(ui, 'fetch_launcher_json', return_value=(None, denied)) as fetch:
            self.poller.start()
            self.complete_poll()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.state['name'], ui.UI_STATE_STALE)
        self.assertTrue(self.root.destroyed)

    def test_fallback_retains_instance_check(self):
        offline = (None, ui.make_launcher_error(ui.ERROR_UNREACHABLE, 'Fixture timeout'))
        with patch.object(ui, 'fetch_launcher_json', side_effect=[offline, ({'instance_id': 'other'}, None)]) as fetch:
            self.poller.start()
            self.complete_poll()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], self.args.status_urls)
        self.assertEqual(self.state['name'], ui.UI_STATE_STALE)
        self.assertTrue(self.root.destroyed)

    def test_late_status_cannot_undo_shutdown_and_failed_shutdown_can_resume(self):
        with patch.object(ui, 'fetch_launcher_json_any', side_effect=self.slow_reply) as fetch:
            self.begin_slow_poll()
            self.state['name'] = ui.UI_STATE_SHUTTING_DOWN
            self.release.set()
            self.join_workers()
            self.root.run_next()
            self.assertEqual(self.state['name'], ui.UI_STATE_SHUTTING_DOWN)
            self.assertEqual(self.status.value, '')
            self.assertFalse(self.shutdown.enabled)
            self.root.run_next()
            self.assertEqual(fetch.call_count, 1)
            self.state['name'] = ui.UI_STATE_OFFLINE
            self.complete_poll()
        self.assertEqual(self.state['name'], ui.UI_STATE_CURRENT)

    def test_worker_exception_is_offline_and_polling_recovers(self):
        with patch.object(ui, 'fetch_launcher_json_any', side_effect=[ValueError('fixture-only'), ({'status': 'ok'}, None)]):
            self.poller.start()
            self.complete_poll()
            self.assertEqual(self.state['name'], ui.UI_STATE_OFFLINE)
            self.assertNotIn('fixture-only', self.status.value)
            self.complete_poll()
        self.assertEqual(self.state['name'], ui.UI_STATE_CURRENT)

    def test_worker_start_failure_does_not_stall_polling(self):
        with patch.object(ui.threading, 'Thread', side_effect=RuntimeError('Fixture thread limit')):
            self.poller.start()
            self.complete_poll()
        self.assertEqual(self.state['name'], ui.UI_STATE_OFFLINE)
        self.assertEqual(len(self.root.callbacks), 1)


@unittest.skipUnless(os.environ.get('STANDTERM_TEST_REAL_TK') == '1', 'Opt-in hidden native Tk check')
class NativeTkPollingTests(unittest.TestCase):
    def test_hidden_tk_event_loop_handles_pending_request_and_close(self):
        for close_pending in (False, True):
            with self.subTest(close_pending=close_pending):
                root = ui.tk.Tk()
                root.withdraw()
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()
                state = {'name': ui.UI_STATE_STARTING, 'offline_polls': 0}
                beats, errors, observed = [], [], []
                timers = []
                poller = None
                root.report_callback_exception = lambda *error: errors.append(error)

                def later(delay, callback):
                    timers.append(root.after(delay, callback))

                def cancel_timers(event):
                    if event.widget is root:
                        for timer in timers:
                            root.after_cancel(timer)

                root.bind('<Destroy>', cancel_timers, add='+')

                def slow_fetch(*_args):
                    entered.set()
                    release.wait(timeout=5)
                    finished.set()
                    return {'status': 'ok'}, None

                def beat():
                    beats.append(1)
                    later(20, beat)

                def check_pending():
                    observed.append((entered.is_set(), len(beats), state['name']))
                    if close_pending:
                        root.destroy()
                    else:
                        release.set()
                        check_complete()

                def check_complete():
                    if state['name'] == ui.UI_STATE_CURRENT:
                        root.destroy()
                    else:
                        later(20, check_complete)

                try:
                    with patch.object(ui, 'fetch_launcher_json_any', side_effect=slow_fetch):
                        poller = ui.LauncherStatusPoller(root, arguments(), ui.tk.StringVar(root), state, [], None)
                        poller.start()
                        beat()
                        later(400, check_pending)
                        later(4000, root.destroy)
                        root.mainloop()
                        release.set()
                        self.assertTrue(finished.wait(timeout=3))
                    self.assertFalse(errors)
                    self.assertTrue(poller.closed)
                    self.assertEqual(len(observed), 1)
                    self.assertTrue(observed[0][0])
                    self.assertGreaterEqual(observed[0][1], 5)
                    self.assertEqual(observed[0][2], ui.UI_STATE_STARTING)
                    self.assertEqual(state['name'], ui.UI_STATE_STARTING if close_pending else ui.UI_STATE_CURRENT)
                finally:
                    release.set()
                    if poller is None or not poller.closed:
                        root.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
