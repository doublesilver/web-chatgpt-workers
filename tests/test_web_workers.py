import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("web_workers", Path(__file__).parents[1] / "scripts" / "web_workers.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class FakeBrowser:
    worktree = "path:/example"
    profile = None

    def __init__(self, fail_send=False, finished=True, wrong_echo=False):
        self.pages = {}
        self.sent = []
        self.configured = []
        self.fail_send = fail_send
        self.finished = finished
        self.wrong_echo = wrong_echo

    def new_tab(self):
        page = "page-" + str(len(self.pages) + 1)
        self.pages[page] = ""
        return page

    def inspect(self, page):
        sent = page in self.sent
        return {"url": "https://chatgpt.com/c/example", "editor": True, "login": False,
                "composer": self.pages[page], "sendReady": not sent,
                "user": ("unrelated" if self.wrong_echo else self.pages[page]) if sent else "",
                "reply": "Verified reply" if sent and self.finished else "",
                "completedControl": self.finished, "generating": sent and not self.finished,
                "responseLinked": True}

    def fill(self, page, prompt):
        if page not in self.configured:
            raise AssertionError("model and reasoning were not verified before filling")
        self.pages[page] = prompt

    def configure_highest(self, page):
        self.configured.append(page)

    def prepare_send(self, page, expected):
        return "send-reference"

    def send(self, page, reference=None):
        self.sent.append(page)
        if self.fail_send:
            raise worker.WorkerError("lost receipt after click")

    def close(self, page):
        self.pages.pop(page)

    def stop(self, page):
        self.finished = True


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def runner(self, browser=None):
        runner = worker.Runner(browser or FakeBrowser(), self.root, interval=0.001)
        runner.acceptance_timeout = 0.015
        return runner

    def tasks(self, count=3):
        return [{"id": f"worker-{n}", "prompt": f"Review bounded topic {n}."} for n in range(count)]

    def test_four_tasks_rejected_before_creating_tabs(self):
        runner = self.runner()
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(4), 1)
        self.assertFalse(runner.browser.pages)
        self.assertFalse((self.root / "active.json").exists())

    def test_send_uses_observed_korean_accessibility_label(self):
        browser = object.__new__(worker.Browser)
        browser.snapshot = lambda page: {"refs": {"e27": {"role": "button", "name": "프롬프트 보내기"}}}
        calls = []
        browser.call = lambda *args, **kwargs: calls.append((args, kwargs))
        browser.inspect = lambda page: {"url": "https://chatgpt.com/", "composer": "expected", "sendReady": True}
        reference = browser.prepare_send("owned-page", "expected")
        browser.send("owned-page", reference)
        self.assertEqual(calls, [(("click", "--element", "@e27"), {"page": "owned-page"})])

    def test_missing_latest_model_blocks_before_submission(self):
        browser = object.__new__(worker.Browser)
        browser.inspect = lambda page: {"url": "https://chatgpt.com/", "composer": "", "login": False}
        snapshots = iter([{"refs": {}}] * 10 + [
            {"refs": {"e1": {"role": "button", "name": "High"}}},
            {"refs": {"e2": {"role": "menuitem", "name": "모델 선택"}}},
            {"refs": {"e3": {"role": "menuitemradio", "name": "GPT-5.6 Sol"}}},
        ])
        browser.snapshot = lambda page: next(snapshots)
        actions = []
        def call(*args, **kwargs):
            actions.append(args[0])
            return {"result": '["High"]'} if args[0] == "eval" else {}
        browser.call = call
        with self.assertRaises(worker.WorkerError):
            browser.configure_highest("owned-page")
        self.assertEqual(actions, ["eval", "click", "click"])

    def test_waits_for_enabled_send_button(self):
        browser = object.__new__(worker.Browser)
        seen = []
        states = iter([False, True])
        def inspect(page):
            ready = next(states)
            seen.append(ready)
            return {"url": "https://chatgpt.com/", "composer": "expected", "sendReady": ready}
        browser.inspect = inspect
        def snapshot(page):
            self.assertTrue(seen[-1], "attempted submission while the send button was disabled")
            return {"refs": {"e27": {"role": "button", "name": "프롬프트 보내기"}}}
        browser.snapshot = snapshot
        with patch.object(worker.time, "sleep"):
            self.assertEqual(browser.prepare_send("owned-page", "expected"), "@e27")

    def test_stop_uses_observed_korean_accessibility_label(self):
        browser = object.__new__(worker.Browser)
        browser.snapshot = lambda page: {"refs": {"e31": {"role": "button", "name": "답변 중지"}}}
        calls = []
        browser.call = lambda *args, **kwargs: calls.append((args, kwargs))
        browser.stop("owned-page")
        self.assertEqual(calls, [(("click", "--element", "@e31"), {"page": "owned-page"})])

    def test_wrong_site_or_changed_prompt_never_clicks(self):
        for state in [
            {"url": "https://example.com/", "composer": "expected"},
            {"url": "https://chatgpt.com/", "composer": "changed"},
            {"url": "https://chatgpt.com/", "composer": "expected", "login": True},
        ]:
            browser = object.__new__(worker.Browser)
            browser.inspect = lambda page, state=state: state
            browser.call = lambda *args, **kwargs: self.fail("browser action on an unverified page")
            with self.assertRaises(worker.WorkerError):
                browser.prepare_send("owned-page", "expected")

    def test_malformed_cli_receipts_are_controlled_errors(self):
        browser = object.__new__(worker.Browser)
        browser.cli, browser.worktree = "orca", None
        for output in ["null", "[]", '{"ok":true}', '{"ok":"false","result":{}}']:
            with patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(worker.WorkerError):
                    browser.call("snapshot")

    def test_three_tasks_collected_and_echo_verified(self):
        runner = self.runner()
        path, result = runner.start(self.tasks(), 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(runner.browser.sent), 3)
        self.assertTrue(all(task["submission_verified"] for task in result["tasks"]))
        self.assertEqual(len(list(path.glob("*.txt"))), 3)
        self.assertEqual(set(runner.browser.configured), set(runner.browser.sent))

    def test_uncertain_submission_is_persisted_and_never_resent(self):
        browser = FakeBrowser(fail_send=True)
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 1)
        path, run = runner.current()
        self.assertEqual(run["tasks"][0]["status"], "submission_uncertain")
        browser.fail_send = False
        _, result = runner.collect(path, run, 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(browser.sent), 1)

    def test_timed_out_batch_blocks_another_batch(self):
        runner = self.runner(FakeBrowser(finished=False))
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(), 0.015)
        self.assertEqual(runner.current()[1]["observed_generating_peak"], 3)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(), 1)
        self.assertEqual(len(runner.browser.sent), 3)

    def test_partial_batch_resume_sends_only_never_attempted_tasks(self):
        browser = FakeBrowser(fail_send=True)
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(), 1)
        path, run = runner.current()
        browser.fail_send = False
        _, result = runner.resume(path, run, 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(browser.sent), 3)
        self.assertEqual(len(set(browser.sent)), 3)

    def test_passphrases_and_short_credentials_rejected(self):
        for value in ['"alpha bravo charlie delta"', '"seven77"', "'alpha\nbravo'", '"""alpha\nbravo"""']:
            with self.subTest(value_length=len(value)):
                with self.assertRaises(worker.WorkerError):
                    worker.validate_tasks({"tasks": [{"id": "audit", "prompt": "password = " + value}]})
        worker.validate_tasks({"tasks": [{"id": "audit", "prompt": 'password = "<REDACTED>"'}]})

    def test_reply_file_interruption_cannot_persist_complete(self):
        runner = self.runner()
        original = worker.os.open
        def interrupt_answer(path, *args, **kwargs):
            if str(path).endswith(".txt") or str(path).endswith(".txt.tmp"):
                raise KeyboardInterrupt
            return original(path, *args, **kwargs)
        with patch.object(worker.os, "open", side_effect=interrupt_answer):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(self.tasks(1), 1)
        path, run = runner.current()
        self.assertNotEqual(run["tasks"][0]["status"], "complete")
        _, result = runner.collect(path, run, 1)
        self.assertEqual(Path(result["tasks"][0]["answer_file"]).read_text(), "Verified reply\n")
        self.assertEqual(len(runner.browser.sent), 1)

    def test_pending_submission_cannot_be_cancelled_by_closing_tab(self):
        browser = FakeBrowser(finished=False)
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 0.015)
        inspect = browser.inspect
        browser.inspect = lambda page: {**inspect(page), "generating": False}
        path, run = runner.current()
        with self.assertRaises(worker.WorkerError):
            runner.cancel(path, run)
        self.assertEqual(len(browser.pages), 1)
        self.assertNotIn(run["status"], worker.TERMINAL)

    def test_unchanged_composer_after_click_can_be_cancelled(self):
        browser = FakeBrowser()
        browser.send = lambda page, reference=None: None
        inspect = browser.inspect
        browser.inspect = lambda page: {**inspect(page), "user": "", "sendReady": True}
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 1)
        path, run = runner.current()
        self.assertEqual(run["tasks"][0]["status"], "submission_uncertain")
        runner.cancel(path, run)
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(browser.pages)

    def test_old_assistant_turn_cannot_complete_new_request(self):
        browser = FakeBrowser()
        inspect = browser.inspect
        browser.inspect = lambda page: {**inspect(page), "responseLinked": False}
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 0.015)

    def test_cancel_continues_after_one_page_is_unavailable(self):
        browser = FakeBrowser(finished=False)
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(), 0.015)
        inspect = browser.inspect
        def inspect_with_missing_page(page):
            if page == "page-1":
                raise worker.WorkerError("page unavailable")
            return inspect(page)
        browser.inspect = inspect_with_missing_page
        path, run = runner.current()
        with self.assertRaises(worker.WorkerError):
            runner.cancel(path, run)
        self.assertEqual(set(browser.pages), {"page-1"})
        self.assertNotIn(run["status"], worker.TERMINAL)

    def test_login_failure_before_submission_can_be_cancelled(self):
        browser = FakeBrowser()
        inspect = browser.inspect
        browser.inspect = lambda page: {**inspect(page), "login": True}
        runner = self.runner(browser)
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 1)
        path, run = runner.current()
        runner.cancel(path, run)
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(browser.sent)
        self.assertFalse(browser.pages)

    def test_unrelated_reply_cannot_complete_task(self):
        runner = self.runner(FakeBrowser(wrong_echo=True))
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 0.015)
        self.assertNotEqual(runner.current()[1]["status"], "complete")

    def test_click_without_new_message_remains_uncertain(self):
        runner = self.runner(FakeBrowser(wrong_echo=True))
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(1), 0.015)
        self.assertEqual(runner.current()[1]["tasks"][0]["status"], "submission_uncertain")
        self.assertEqual(len(runner.browser.sent), 1)

    def test_proven_unsubmitted_click_is_retried(self):
        browser = FakeBrowser()
        attempts = []
        original_send = browser.send
        def drop_first_click(page, reference=None):
            attempts.append(page)
            if len(attempts) > 1:
                original_send(page, reference)
        browser.send = drop_first_click
        runner = self.runner(browser)
        _, result = runner.start(self.tasks(1), 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(browser.sent), 1)

    def test_owned_composer_mutation_is_restored_and_retried(self):
        browser = FakeBrowser()
        attempts = []
        original_send = browser.send
        def mutate_after_first_click(page, reference=None):
            attempts.append(page)
            if len(attempts) == 1:
                browser.pages[page] += " ㄷ"
            else:
                original_send(page, reference)
        browser.send = mutate_after_first_click
        runner = self.runner(browser)
        _, result = runner.start(self.tasks(1), 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(attempts), 2)

    def test_global_lock_rejects_another_runner(self):
        first, second = self.runner(), self.runner()
        with first.locked():
            with self.assertRaises(worker.WorkerError):
                with second.locked():
                    self.fail("second runner acquired lock")

    def test_secret_shapes_and_unsafe_ids_rejected(self):
        for text in ["sk-" + "X" * 40, "Bearer " + "X" * 30, "password=" + "x" * 12]:
            with self.subTest(kind=text[:3]):
                with self.assertRaises(worker.WorkerError) as error:
                    worker.validate_tasks({"tasks": [{"id": "audit", "prompt": text}]})
                self.assertNotIn(text, str(error.exception))
        with self.assertRaises(worker.WorkerError):
            worker.validate_tasks({"tasks": [{"id": "../escape", "prompt": "Review."}]})

    def test_cancel_stops_only_owned_tabs(self):
        runner = self.runner(FakeBrowser(finished=False))
        with self.assertRaises(worker.WorkerError):
            runner.start(self.tasks(), 0.015)
        runner.browser.pages["user-tab"] = "unrelated conversation"
        path, run = runner.current()
        runner.cancel(path, run)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(runner.browser.pages, {"user-tab": "unrelated conversation"})


if __name__ == "__main__":
    unittest.main()
