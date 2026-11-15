"""Tests for `shared.prompt`: autofill, non-TTY fallback, choices, `raw`, and UI live pause."""

from __future__ import annotations

import contextvars
import io
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from unittest import mock

import questionary

import shared
from shared import prompt


class _FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _FakeQuestion:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error

    def unsafe_ask(self) -> object:
        if self._error is not None:
            raise self._error
        return self._result


class AutofillAndFallbackTest(unittest.TestCase):
    def test_error_hierarchy_and_raw(self) -> None:
        self.assertTrue(issubclass(prompt.Error, shared.Error))
        self.assertIs(prompt.raw, questionary)

    def test_non_tty_returns_defaults_or_raises(self) -> None:
        with (
            mock.patch("sys.stdin", _FakeStream(False)),
            mock.patch("sys.stdout", _FakeStream(False)),
        ):
            self.assertEqual(prompt.ask("Name", default="ada"), "ada")
            self.assertTrue(prompt.confirm("Continue?"))
            self.assertFalse(prompt.confirm("Continue?", default=False))
            self.assertEqual(prompt.choose("Color", ["red", "blue"], default="blue"), "blue")
            self.assertEqual(
                prompt.choose("Tags", ["a", "b"], many=True, default=("b",)), ["b"]
            )
            with self.assertRaises(prompt.Error):
                prompt.ask("Token")
            with self.assertRaises(prompt.Error):
                prompt.choose("Color", ["red", "blue"])

    def test_autofill_context_overrides_tty(self) -> None:
        with (
            mock.patch("sys.stdin", _FakeStream(True)),
            mock.patch("sys.stdout", _FakeStream(True)),
            prompt.autofill(),
        ):
            self.assertEqual(prompt.ask("Name", default="ada"), "ada")
            self.assertFalse(prompt.confirm("Deploy?", default=False))
            self.assertEqual(prompt.choose("Mode", {"Fast": 1, "Safe": 2}, default=2), 2)
            self.assertEqual(
                prompt.choose("Ops", {"A": 1, "B": 2}, many=True, default=[1]), [1]
            )
            with self.assertRaises(prompt.Error):
                prompt.ask("Secret", secret=True)

    def test_autofill_isolated_across_threads(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        other_result: list[str] = []

        def worker() -> None:
            entered.wait()
            with (
                mock.patch("sys.stdin", _FakeStream(True)),
                mock.patch("sys.stdout", _FakeStream(True)),
                mock.patch("questionary.text", return_value=_FakeQuestion("typed")),
            ):
                other_result.append(prompt.ask("Name", default="fallback"))
            release.set()

        thread = threading.Thread(target=worker)
        thread.start()
        with prompt.autofill():
            entered.set()
            release.wait()
            self.assertEqual(prompt.ask("Name", default="fallback"), "fallback")
        thread.join()
        self.assertEqual(other_result, ["typed"])

    def test_autofill_isolated_in_copied_context(self) -> None:
        ctx = contextvars.copy_context()
        with prompt.autofill():
            with (
                mock.patch("sys.stdin", _FakeStream(True)),
                mock.patch("sys.stdout", _FakeStream(True)),
                mock.patch("questionary.text", return_value=_FakeQuestion("typed")),
            ):
                self.assertEqual(ctx.run(lambda: prompt.ask("Name", default="fallback")), "typed")
                self.assertEqual(prompt.ask("Name", default="fallback"), "fallback")


class ChoiceValidationTest(unittest.TestCase):
    def test_empty_choices_raise(self) -> None:
        with self.assertRaises(prompt.Error):
            prompt.choose("Pick", [])
        with self.assertRaises(prompt.Error):
            prompt.choose("Pick", {}, many=True)

    def test_invalid_defaults_raise(self) -> None:
        with self.assertRaises(prompt.Error):
            prompt.choose("Pick", ["a", "b"], default="c")
        with self.assertRaises(prompt.Error):
            prompt.choose("Pick", ["a", "b"], many=True, default=("c",))


class InteractiveQuestionaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._stdin = mock.patch("sys.stdin", _FakeStream(True))
        self._stdout = mock.patch("sys.stdout", _FakeStream(True))
        self._stdin.start()
        self._stdout.start()

    def tearDown(self) -> None:
        self._stdout.stop()
        self._stdin.stop()

    def test_ask_and_secret_and_validator(self) -> None:
        captured: dict[str, object] = {}

        def fake_text(message: str, **kwargs: object) -> _FakeQuestion:
            captured["message"] = message
            captured["kwargs"] = kwargs
            return _FakeQuestion("sam")

        def nonempty(val: str) -> bool | str:
            return True if val else "required"

        with mock.patch("questionary.text", side_effect=fake_text):
            ans = prompt.ask("Name", default="ada", validate=nonempty)
        self.assertEqual(ans, "sam")
        self.assertEqual(captured["message"], "Name")
        kw = captured["kwargs"]
        assert isinstance(kw, dict)
        self.assertEqual(kw["default"], "ada")
        validator = kw["validate"]
        assert callable(validator)
        self.assertTrue(validator("ok"))
        self.assertEqual(validator(""), "required")

        with mock.patch("questionary.password", return_value=_FakeQuestion("pw")) as pw_mock:
            self.assertEqual(prompt.ask("Token", secret=True), "pw")
        pw_mock.assert_called_once()

    def test_confirm_choose_and_choose_many(self) -> None:
        with mock.patch("questionary.confirm", return_value=_FakeQuestion(False)) as conf:
            self.assertFalse(prompt.confirm("Proceed?", default=False))
        conf.assert_called_once()
        self.assertEqual(conf.call_args.args, ("Proceed?",))
        self.assertFalse(conf.call_args.kwargs["default"])
        self.assertEqual(conf.call_args.kwargs["qmark"], "•")

        with mock.patch("questionary.select", return_value=_FakeQuestion(2)) as sel:
            self.assertEqual(prompt.choose("Env", {"Dev": 1, "Prod": 2}, default=2), 2)
        items = sel.call_args.kwargs["choices"]
        self.assertEqual([c.title for c in items], ["Dev", "Prod"])
        self.assertIs(sel.call_args.kwargs["default"], items[1])

        with mock.patch("questionary.checkbox", return_value=_FakeQuestion([1, 2])) as chk:
            self.assertEqual(
                prompt.choose("Features", {"A": 1, "B": 2}, many=True, default=[1]),
                [1, 2],
            )
        citems = chk.call_args.kwargs["choices"]
        self.assertTrue(citems[0].checked)
        self.assertFalse(citems[1].checked)

    def test_eof_raises_prompt_error_and_interrupt_propagates(self) -> None:
        with mock.patch("questionary.text", return_value=_FakeQuestion(error=EOFError())):
            with self.assertRaises(prompt.Error):
                prompt.ask("Name")
        with mock.patch("questionary.text", return_value=_FakeQuestion(error=KeyboardInterrupt())):
            with self.assertRaises(KeyboardInterrupt):
                prompt.ask("Name")

    def test_pauses_ui_live_only_when_loaded(self) -> None:
        events: list[str] = []

        @contextmanager
        def fake_pause():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        fake_ui_mod = types.ModuleType("shared._ui")
        fake_ui_mod.ui = types.SimpleNamespace(pause_live=fake_pause)  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"shared._ui": fake_ui_mod}):
            with mock.patch("questionary.confirm", return_value=_FakeQuestion(True)):
                self.assertTrue(prompt.confirm("Ok?"))
        self.assertEqual(events, ["enter", "exit"])

        raw_events: list[str] = []
        fake_live = types.SimpleNamespace(
            is_started=True,
            stop=lambda: raw_events.append("stop"),
            start=lambda: raw_events.append("start"),
        )
        fake_raw_mod = types.ModuleType("shared._ui")
        fake_raw_mod.ui = types.SimpleNamespace(  # type: ignore[attr-defined]
            raw=types.SimpleNamespace(_live=fake_live)
        )
        with mock.patch.dict(sys.modules, {"shared._ui": fake_raw_mod}):
            with mock.patch("questionary.confirm", return_value=_FakeQuestion(True)):
                self.assertTrue(prompt.confirm("Ok?"))
        self.assertEqual(raw_events, ["stop", "start"])

        without_ui = {k: v for k, v in sys.modules.items() if k != "shared._ui"}
        with mock.patch.dict(sys.modules, without_ui, clear=True):
            with mock.patch("questionary.confirm", return_value=_FakeQuestion(True)):
                self.assertTrue(prompt.confirm("Ok?"))
            self.assertNotIn("shared._ui", sys.modules)
