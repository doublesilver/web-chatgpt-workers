#!/usr/bin/env python3
"""Run up to three ChatGPT website conversations through Orca's public CLI."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse


MAX_WORKERS = 3
ROOT = Path.home() / ".codex" / "web-workers"
TERMINAL = {"complete", "cancelled"}
PAGE_STATE = r"""JSON.stringify((() => {
  const visible = e => !!e && e.getClientRects().length > 0;
  const buttons = Array.from(document.querySelectorAll('button'));
  const stop = buttons.find(e => visible(e) &&
    (e.dataset.testid === 'stop-button' || /^(Stop generating|생성 중지|응답 중지|답변 중지)$/i.test(e.getAttribute('aria-label') || '')));
  const send = buttons.find(e => visible(e) &&
    (e.dataset.testid === 'send-button' || /^(Send prompt|Send message|프롬프트 보내기|프롬프트 전송|메시지 보내기)$/i.test(e.getAttribute('aria-label') || '')));
  const assistant = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]')).at(-1);
  const article = assistant && (assistant.closest('[data-testid^="conversation-turn-"]') || assistant.closest('article'));
  const user = Array.from(document.querySelectorAll('[data-message-author-role="user"]')).at(-1);
  const editor = document.querySelector('#prompt-textarea');
  const login = visible(document.querySelector('[data-testid="login-button"]')) ||
    visible(document.querySelector('input[autocomplete="email"]'));
  const alerts = Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(e => e.innerText).join('\n');
  return {
    url: location.origin + location.pathname, title: document.title,
    login: !!login, editor: !!editor && visible(editor),
    composer: editor ? Array.from(editor.children).map(e => e.textContent).join('\n').replace(/\u00a0/g, ' ') : '', generating: !!stop,
    sendReady: !!send && !send.disabled,
    user: user ? user.innerText : '', reply: assistant ? assistant.innerText : '',
    responseLinked: !!user && !!assistant && !!(user.compareDocumentPosition(assistant) & Node.DOCUMENT_POSITION_FOLLOWING),
    userMessageId: user?.getAttribute('data-message-id'), assistantMessageId: assistant?.getAttribute('data-message-id'),
    completedControl: !!article && Array.from(article.querySelectorAll('button')).some(e =>
      e.dataset.testid === 'copy-turn-action-button' || /^(Copy|복사)$/i.test(e.getAttribute('aria-label') || '')),
    blocker: /too many requests|usage limit|reached.{0,40}limit|한도.{0,20}도달|사용량.{0,20}제한/i.test(alerts) ? 'usage_limit' :
      /unusual activity|verify you are human|인간인지 확인|추가 인증|just a moment/i.test(document.title + '\n' + alerts) ? 'verification_required' :
      alerts ? 'page_alert' : null
  };
})())"""


class WorkerError(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def write_answer(path, reply):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(reply + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def check_page(state):
    address = urlparse(state.get("url", ""))
    if address.scheme != "https" or address.hostname != "chatgpt.com":
        raise WorkerError("ChatGPT page redirected; inspect the existing tab before proceeding")
    if state.get("login"):
        raise WorkerError("ChatGPT login required in the open Orca tab; sign in manually")
    if state.get("blocker"):
        raise WorkerError("ChatGPT page blocked: " + state["blocker"])


def validate_tasks(document):
    tasks = document.get("tasks") if isinstance(document, dict) else None
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_WORKERS:
        raise WorkerError("tasks must contain between 1 and 3 tasks; nothing was sent")
    seen = set()
    patterns = {
        "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "API key": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
        "AWS access key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        "bearer credential": r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}",
    }
    for task in tasks:
        if not isinstance(task, dict):
            raise WorkerError("each task must be an object")
        name, prompt = task.get("id"), task.get("prompt")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise WorkerError("task IDs must be short, filename-safe names")
        if name in seen:
            raise WorkerError("task IDs must be unique")
        seen.add(name)
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkerError("every task needs a non-empty prompt")
        for kind, pattern in patterns.items():
            if re.search(pattern, prompt):
                raise WorkerError(f"task {name}: possible {kind}; redact it before sending")
        assignments = re.finditer(r'''(?im)["']?\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key)["']?\s*[:=][ \t]*(.*)''', prompt)
        for match in assignments:
            value = match.group(1).strip().rstrip(",;").strip().strip("\"'")
            masked = value in {"<REDACTED>", "[REDACTED]", "REDACTED"} or re.fullmatch(r"\*{3,}", value)
            if not masked:
                raise WorkerError(f"task {name}: credential assignment must be explicitly redacted")
    return tasks


class Browser:
    def __init__(self, worktree, profile=None):
        configured = os.environ.get("ORCA_CLI_COMMAND")
        self.cli = configured or ("orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else
                                  "orca-ide" if sys.platform.startswith("linux") and not os.environ.get("ORCA_TERMINAL_ID") else "orca")
        if not shutil.which(self.cli):
            raise WorkerError(f"Orca CLI unavailable: {self.cli}")
        self.worktree, self.profile = worktree, profile

    def call(self, *args, page=None):
        command = [self.cli, *args, "--json"]
        if self.worktree:
            command += ["--worktree", self.worktree]
        if page:
            command += ["--page", page]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=55)
        except subprocess.TimeoutExpired as error:
            raise WorkerError("Orca command timed out; its effect is uncertain, do not resend") from error
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise WorkerError(f"Orca returned invalid JSON (exit {process.returncode}); inspect the page") from error
        if not isinstance(response, dict):
            raise WorkerError("Orca returned an invalid response object; its effect is uncertain")
        if process.returncode or response.get("ok") is not True:
            failure = response.get("error", {})
            code = failure.get("code", "unknown") if isinstance(failure, dict) else "unknown"
            raise WorkerError(f"Orca failed: {code}; inspect the existing page before retrying")
        if "result" not in response:
            raise WorkerError("Orca response omitted result; its effect is uncertain")
        return response["result"]

    def new_tab(self):
        args = ["tab", "create", "--url", "https://chatgpt.com/"]
        if self.profile:
            args += ["--profile", self.profile]
        return self.call(*args)["browserPageId"]

    def inspect(self, page):
        response = self.call("eval", "--expression", PAGE_STATE, page=page)
        try:
            state = response["result"]
            state = json.loads(state) if isinstance(state, str) else state
            if not isinstance(state, dict) or "url" not in state:
                raise ValueError("missing page state")
            return state
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerError("Orca returned invalid page state; inspect the existing tab") from error

    def snapshot(self, page):
        return self.call("snapshot", page=page)

    def configure_highest(self, page):
        check_page(self.inspect(page))

        # The unified Chat UI no longer exposes a model selector. In that UI,
        # Chat is the current model surface and the only selectable control is
        # the reasoning level. Verify both states directly instead of looking
        # for the removed "Select model" submenu.
        for _ in range(10):
            unified = self.snapshot(page)
            shown = unified.get("snapshot", "")
            chat_selected = re.search(r'radio "(?:Chat|채팅)" \[checked=true', shown, re.I)
            highest_reasoning = re.search(r'button "(?:매우 높음|Very high)"', shown, re.I)
            if chat_selected and highest_reasoning:
                check_page(self.inspect(page))
                return
            time.sleep(0.5)

        def unique_ref(snapshot, role, pattern):
            matches = [ref for ref, data in snapshot.get("refs", {}).items()
                       if data.get("role") == role and re.fullmatch(pattern, data.get("name", ""), re.I)]
            if len(matches) != 1:
                raise WorkerError(f"ChatGPT {role} for highest model was not uniquely identified; nothing was sent")
            return "@" + matches[0].lstrip("@")

        def open_menu():
            closed = self.snapshot(page)
            expression = r'''JSON.stringify(Array.from(document.querySelectorAll('form button[aria-haspopup="menu"]'))
              .filter(b => b.dataset.testid !== 'composer-plus-btn' && b.getClientRects().length)
              .map(b => b.innerText.replace(/\s+/g, ' ').trim()))'''
            try:
                labels = json.loads(self.call("eval", "--expression", expression, page=page)["result"])
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise WorkerError("ChatGPT model button could not be identified; nothing was sent") from error
            if not isinstance(labels, list) or len(labels) != 1 or not isinstance(labels[0], str) or not labels[0]:
                raise WorkerError("ChatGPT model button could not be identified; nothing was sent")
            button = unique_ref(closed, "button", re.escape(labels[0]))
            self.call("click", "--element", button, page=page)
            return self.snapshot(page)

        menu = open_menu()
        self.call("click", "--element", unique_ref(menu, "menuitem", r"모델 선택|Select model"), page=page)
        models = self.snapshot(page)
        latest = unique_ref(models, "menuitemradio", r"최신|Latest")
        if not re.search(r'menuitemradio "(?:최신|Latest)" \[checked=true', models.get("snapshot", ""), re.I):
            self.call("click", "--element", latest, page=page)
        self.call("keypress", "--key", "Escape", page=page)

        menu = open_menu()
        def position(snapshot):
            shown = snapshot.get("snapshot", "")
            korean = re.search(r"(\d+)개 중 (\d+)번째", shown)
            english = re.search(r"(\d+) of (\d+)", shown, re.I)
            if korean:
                total, current = map(int, korean.groups())
            elif english:
                current, total = map(int, english.groups())
            else:
                raise WorkerError("ChatGPT performance level could not be verified; nothing was sent")
            if not 1 <= current <= total <= 10:
                raise WorkerError("ChatGPT performance level is invalid; nothing was sent")
            return current, total

        current, total = position(menu)
        if current < total:
            self.call("click", "--element", unique_ref(menu, "menuitem", r"성능|Performance"), page=page)
            menu = self.snapshot(page)
            current, total = position(menu)
            for _ in range(total - current):
                self.call("keypress", "--key", "ArrowRight", page=page)
                menu = self.snapshot(page)
                advanced, observed_total = position(menu)
                if advanced != current + 1 or observed_total != total:
                    raise WorkerError("ChatGPT performance did not advance; nothing was sent")
                current = advanced
        if current != total:
            raise WorkerError("ChatGPT highest performance was not selected; nothing was sent")
        self.call("keypress", "--key", "Escape", page=page)
        menu = open_menu()
        self.call("click", "--element", unique_ref(menu, "menuitem", r"모델 선택|Select model"), page=page)
        models = self.snapshot(page)
        unique_ref(models, "menuitemradio", r"최신|Latest")
        if not re.search(r'menuitemradio "(?:최신|Latest)" \[checked=true', models.get("snapshot", ""), re.I):
            raise WorkerError("ChatGPT latest model was not selected; nothing was sent")
        self.call("keypress", "--key", "Escape", page=page)
        check_page(self.inspect(page))

    def fill(self, page, text):
        check_page(self.inspect(page))
        refs = self.snapshot(page).get("refs", {})
        candidates = [ref for ref, data in refs.items() if data.get("role") == "textbox" and
                      re.search(r"ChatGPT|Message|메시지|채팅", data.get("name", ""), re.I)]
        if len(candidates) != 1:
            raise WorkerError("ChatGPT composer not uniquely identified; inspect its fresh snapshot")
        self.call("fill", "--element", "@" + candidates[0].lstrip("@"), "--value", text, page=page)
        if self.inspect(page)["composer"].strip() != text.strip():
            raise WorkerError("composer text did not match the task; nothing was submitted")

    def prepare_send(self, page, expected):
        deadline = time.monotonic() + 45
        while True:
            state = self.inspect(page)
            check_page(state)
            if state.get("generating") or state.get("composer", "").strip() != expected.strip():
                raise WorkerError("composer changed before submission; nothing was sent")
            if state.get("sendReady"):
                break
            if time.monotonic() >= deadline:
                raise WorkerError("send button remained disabled; nothing was submitted")
            time.sleep(0.5)
        refs = self.snapshot(page).get("refs", {})
        candidates = [ref for ref, data in refs.items() if data.get("role") == "button" and
                      re.fullmatch(r"Send prompt|Send message|프롬프트 보내기|프롬프트 전송|메시지 보내기", data.get("name", ""), re.I)]
        if len(candidates) != 1:
            raise WorkerError("send button not uniquely identified; nothing was submitted")
        return "@" + candidates[0].lstrip("@")

    def send(self, page, reference):
        self.call("click", "--element", reference, page=page)

    def stop(self, page):
        refs = self.snapshot(page).get("refs", {})
        candidates = [ref for ref, data in refs.items() if data.get("role") == "button" and
                      re.fullmatch(r"Stop generating|생성 중지|응답 중지|답변 중지", data.get("name", ""), re.I)]
        if len(candidates) != 1:
            raise WorkerError("stop button not uniquely identified; leave this run unresolved")
        self.call("click", "--element", "@" + candidates[0].lstrip("@"), page=page)

    def close(self, page):
        self.call("tab", "close", page=page)


class Runner:
    def __init__(self, browser, root=ROOT, interval=2):
        self.browser, self.root, self.interval = browser, Path(root), interval
        self.acceptance_timeout = 20
        self.max_proven_unsubmitted_retries = 2

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkerError("another web worker runner is active; maximum is three") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def current(self):
        pointer = self.root / "active.json"
        if not pointer.exists():
            return None
        path = Path(json.loads(pointer.read_text())["run"])
        return path, json.loads((path / "result.json").read_text())

    def save(self, path, run):
        write_json(path / "result.json", run)

    def ready(self, page, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.browser.inspect(page)
            check_page(state)
            if state.get("editor"):
                return state
            time.sleep(self.interval)
        raise WorkerError("ChatGPT composer unavailable; inspect the open tab for login or verification")

    def start(self, tasks, timeout):
        tasks = validate_tasks({"tasks": tasks})
        previous = self.current()
        if previous and previous[1]["status"] not in TERMINAL:
            raise WorkerError(f"an unresolved run already exists: {previous[0]}; resume or cancel it")
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        path = self.root / "runs" / name
        run = {"id": name, "status": "starting", "started_at": timestamp(),
               "worktree": self.browser.worktree, "profile": self.browser.profile,
               "observed_generating_peak": 0, "tasks": []}
        for task in tasks:
            marker = f"[Codex web worker {name}/{task['id']}]"
            prompt = marker + "\nYou are an independent analysis worker for Codex. Work only on the brief below. " + \
                "You cannot access or modify the caller's local files. Do not claim to have run local tests. " + \
                "Use the supplied context, distinguish evidence from assumptions, and return your findings here.\n\n" + task["prompt"]
            run["tasks"].append({"id": task["id"], "marker": marker, "prompt": prompt,
                                 "page": None, "status": "queued"})
        self.save(path, run)
        write_json(self.root / "active.json", {"run": str(path)})
        print(json.dumps({"event": "run_created", "run": str(path), "workers": len(tasks)}), flush=True)
        return self.resume(path, run, timeout)

    def verify_submission(self, path, run, task, reference=None):
        if reference is not None:
            task["submission_attempts"] = task.get("submission_attempts", 0) + 1
            self.save(path, run)
            self.browser.send(task["page"], reference)
        deadline = time.monotonic() + self.acceptance_timeout
        while True:
            state = self.browser.inspect(task["page"])
            check_page(state)
            if task["marker"] in state.get("user", ""):
                task["submission_verified"] = True
                return
            if time.monotonic() >= deadline:
                composer = state.get("composer", "").strip()
                proven_unsubmitted = (not state.get("user") and not state.get("generating") and
                                      task["marker"] in composer and state.get("sendReady"))
                retries = max(0, task.get("submission_attempts", 0) - 1)
                if proven_unsubmitted and retries < self.max_proven_unsubmitted_retries:
                    if composer != task["prompt"].strip():
                        self.browser.fill(task["page"], task["prompt"])
                    reference = self.browser.prepare_send(task["page"], task["prompt"])
                    task["submission_attempts"] = task.get("submission_attempts", 0) + 1
                    task["last_safe_retry_at"] = timestamp()
                    self.save(path, run)
                    self.browser.send(task["page"], reference)
                    deadline = time.monotonic() + self.acceptance_timeout
                    continue
                raise WorkerError("click did not produce a matching user message; submission remains uncertain, do not resend")
            time.sleep(self.interval)

    def resume(self, path, run, timeout):
        if any(task.get("cancel_requested") and task["status"] not in TERMINAL for task in run["tasks"]):
            raise WorkerError("cancellation was requested; finish cancel instead of collecting a partial reply")
        try:
            for task in run["tasks"]:
                if task["status"] == "submission_uncertain":
                    self.verify_submission(path, run, task)
                    task["status"] = "submitted"
                    self.save(path, run)
                    print(json.dumps({"event": "submitted", "worker": task["id"], "page": task["page"]}), flush=True)
                    continue
                if task["status"] != "queued":
                    continue
                if not task.get("page"):
                    task["page"] = self.browser.new_tab()
                    self.save(path, run)
                state = self.ready(task["page"])
                if state.get("user") or state.get("generating"):
                    raise WorkerError("unsubmitted task page already contains a conversation; inspect it before proceeding")
                self.browser.configure_highest(task["page"])
                self.browser.fill(task["page"], task["prompt"])
                reference = self.browser.prepare_send(task["page"], task["prompt"])
                # Persist before clicking: a lost receipt must never cause a duplicate prompt.
                task["status"] = "submission_uncertain"
                task["submitted_at"] = timestamp()
                self.save(path, run)
                self.verify_submission(path, run, task, reference)
                task["status"] = "submitted"
                self.save(path, run)
                print(json.dumps({"event": "submitted", "worker": task["id"], "page": task["page"]}), flush=True)
            return self.collect(path, run, timeout)
        except (WorkerError, OSError, KeyboardInterrupt) as error:
            if run["status"] != "timed_out":
                run["status"] = "needs_attention"
            run["last_error"] = str(error) or "interrupted"
            self.save(path, run)
            raise

    def collect(self, path, run, timeout):
        deadline = time.monotonic() + timeout
        stable = {}
        run["status"] = "collecting"
        self.save(path, run)
        while time.monotonic() < deadline:
            generating = 0
            for task in run["tasks"]:
                if task["status"] == "complete":
                    answer = path / (task["id"] + ".txt")
                    if not task.get("reply"):
                        task["status"] = "submitted"
                    else:
                        if not answer.exists() or answer.read_text() != task["reply"] + "\n":
                            write_answer(answer, task["reply"])
                        task["answer_file"] = str(answer)
                        continue
                if task.get("cancel_requested"):
                    raise WorkerError("cancellation is unresolved; finish cancel before starting a new run")
                if task["status"] == "cancelled":
                    continue
                if not task.get("page") or task["status"] == "queued":
                    raise WorkerError("a task was not submitted; cancel this run before preparing a new batch")
                state = self.browser.inspect(task["page"])
                check_page(state)
                generating += int(bool(state.get("generating")))
                task["url"] = state.get("url")
                if task["marker"] not in state.get("user", ""):
                    stable.pop(task["id"], None)
                    continue
                task["submission_verified"] = True
                task["user_message_id"] = state.get("userMessageId")
                reply = state.get("reply", "").strip()
                finished = reply and not state.get("generating") and state.get("completedControl") and state.get("responseLinked")
                if finished and stable.get(task["id"]) == reply:
                    answer = path / (task["id"] + ".txt")
                    write_answer(answer, reply)
                    task["status"] = "complete"
                    task["completed_at"] = timestamp()
                    task["reply"] = reply
                    task["assistant_message_id"] = state.get("assistantMessageId")
                    task["answer_file"] = str(answer)
                    print(json.dumps({"event": "complete", "worker": task["id"], "answer_file": str(answer)}), flush=True)
                    self.save(path, run)
                    try:
                        self.browser.close(task["page"])
                    except WorkerError as error:
                        task["tab_close_error"] = str(error)
                        print(json.dumps({"event": "tab_close_failed", "worker": task["id"]}), flush=True)
                    else:
                        task["tab_closed_at"] = timestamp()
                        print(json.dumps({"event": "tab_closed", "worker": task["id"]}), flush=True)
                    self.save(path, run)
                elif finished:
                    stable[task["id"]] = reply
                else:
                    stable.pop(task["id"], None)
            run["observed_generating_peak"] = max(run["observed_generating_peak"], generating)
            self.save(path, run)
            if all(task["status"] == "complete" for task in run["tasks"]):
                run["status"] = "complete"
                run["completed_at"] = timestamp()
                self.save(path, run)
                return path, run
            time.sleep(self.interval)
        run["status"] = "timed_out"
        self.save(path, run)
        raise WorkerError(f"response collection timed out; resume the existing run: {path}")

    def cancel(self, path, run):
        run["status"] = "cancelling"
        self.save(path, run)
        errors = []
        for task in run["tasks"]:
            if task["status"] in TERMINAL:
                continue
            try:
                if task.get("page"):
                    if task["status"] != "queued":
                        state = self.browser.inspect(task["page"])
                        check_page(state)
                        task["cancel_requested"] = True
                        self.save(path, run)
                        if state.get("generating"):
                            self.browser.stop(task["page"])
                            for _ in range(10):
                                time.sleep(self.interval)
                                state = self.browser.inspect(task["page"])
                                check_page(state)
                                if not state.get("generating"):
                                    break
                            else:
                                raise WorkerError("generation did not stop; leave this run unresolved")
                        elif (task["status"] == "submission_uncertain" and not state.get("user") and
                              state.get("composer", "").strip() == task["prompt"].strip() and state.get("sendReady")):
                            pass  # The original prompt is still sendable, so this click did not submit it.
                        elif not (state.get("completedControl") and state.get("responseLinked") and task["marker"] in state.get("user", "")):
                            raise WorkerError("submission may still be pending; its slot remains reserved")
                    self.browser.close(task["page"])
                task["status"] = "cancelled"
                task.pop("error", None)
            except WorkerError as error:
                task["error"] = str(error)
                errors.append(task["id"])
            finally:
                self.save(path, run)
        if errors:
            raise WorkerError("cancellation remains unresolved for: " + ", ".join(errors))
        run["status"] = "cancelled"
        self.save(path, run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "run", "status", "resume", "cancel"])
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--worktree", default="path:" + os.getcwd())
    parser.add_argument("--profile")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise WorkerError("timeout must be positive")
        tasks = None
        if args.action == "run":
            if not args.tasks:
                raise WorkerError("run requires --tasks /absolute/path/tasks.json")
            tasks = validate_tasks(json.loads(args.tasks.read_text()))
        browser = Browser(args.worktree, args.profile)
        runner = Runner(browser)
        if args.action == "status":
            current = runner.current()
            if not current:
                print(json.dumps({"status": "idle"}))
            else:
                path, run = current
                print(json.dumps({"run": str(path), "status": run["status"], "tasks": [
                    {key: task[key] for key in ("id", "status", "page", "url", "answer_file", "tab_closed_at", "tab_close_error") if key in task}
                    for task in run["tasks"]]}, ensure_ascii=False, indent=2))
            return 0
        with runner.locked():
            if args.action == "doctor":
                tabs = browser.call("tab", "list").get("tabs", [])
                page = next((tab["browserPageId"] for tab in tabs if str(tab.get("url", "")).startswith("https://chatgpt.com/")), None)
                page = page or browser.new_tab()
                runner.ready(page)
                print(json.dumps({"status": "ready", "page": page, "max_workers": MAX_WORKERS}))
                return 0
            if args.action == "run":
                path, run = runner.start(tasks, args.timeout)
            else:
                if not args.run:
                    raise WorkerError("resume/cancel requires --run /absolute/run/directory")
                path = args.run.resolve()
                if not path.is_relative_to((ROOT / "runs").resolve()):
                    raise WorkerError("run directory must belong to this runner")
                run = json.loads((path / "result.json").read_text())
                browser.worktree, browser.profile = run["worktree"], run.get("profile")
                current = runner.current()
                if current and current[0].resolve() != path:
                    raise WorkerError("only the current run may be resumed or cancelled")
                if args.action == "cancel":
                    runner.cancel(path, run)
                elif run["status"] not in TERMINAL:
                    path, run = runner.resume(path, run, args.timeout)
            print(json.dumps({"status": run["status"], "run": str(path),
                              "observed_generating_peak": run.get("observed_generating_peak", 0)}, ensure_ascii=False))
        return 0
    except (WorkerError, OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "reason": "use status, then resume or cancel; do not resend"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
