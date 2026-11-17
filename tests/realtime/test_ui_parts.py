import gc
import inspect
import weakref

import pytest
from PyQt5.QtWidgets import QApplication, QPushButton, QWidget

from realtime.ui_parts import UiPart, install_parts


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _ClickPart(UiPart):
    LIMIT = 3

    def _on_click(self):
        self._host.clicks += 1

    def add(self, value, step=2, *, scale=10, **extra):
        return (value + step) * scale + len(extra)

    @property
    def doubled(self):
        return self._host.clicks * 2

    @doubled.setter
    def doubled(self, value):
        self._host.clicks = value // 2

    @staticmethod
    def label(text):
        return f"<{text}>"

    def _calls_override(self):
        return self._host._overridden()

    def _overridden(self):
        return "part"


class _Host(QWidget):
    def __init__(self):
        super().__init__()
        self.clicks = 0
        self.button = QPushButton(self)
        self.button.clicked.connect(self._on_click)


install_parts(_Host, (_ClickPart,))


class _Child(_Host):
    def _overridden(self):
        return "child"


def test_button_signal_extra_argument_is_dropped_like_a_plain_method(qapp):
    host = _Host()
    host.button.click()
    host.button.click()
    assert host.clicks == 2
    host.close()


def test_forwarder_keeps_parameters_and_defaults():
    assert inspect.signature(_Host.add, follow_wrapped=False) == inspect.signature(_ClickPart.add)
    host = _Host.__new__(_Host)
    assert _Host.add(host, 1) == 30
    assert _Host.add(host, 1, 3, scale=1, a=1) == 5
    with pytest.raises(TypeError):
        _Host.add(host)


def test_properties_static_methods_and_constants_reach_the_window(qapp):
    host = _Host()
    host.clicks = 4
    assert host.doubled == 8
    host.doubled = 2
    assert host.clicks == 1
    assert host.label("x") == "<x>"
    assert _Host.LIMIT == 3
    host.close()


def test_subclass_override_wins_inside_part_code(qapp):
    child = _Child()
    assert child._calls_override() == "child"
    child.close()


def test_window_and_part_form_no_reference_cycle(qapp):
    host = _Host()
    host.button.click()
    probe = weakref.ref(host)
    gc.disable()
    try:
        del host
        assert probe() is None
    finally:
        gc.enable()


def test_duplicate_names_are_refused():
    class First(UiPart):
        def shared(self):
            pass

    class Second(UiPart):
        def shared(self):
            pass

    class Window:
        def own(self):
            pass

    class OwnPart(UiPart):
        def own(self):
            pass

    with pytest.raises(TypeError, match="both"):
        install_parts(type("A", (), {}), (First, Second))
    with pytest.raises(TypeError, match="window"):
        install_parts(Window, (OwnPart,))


def test_window_classes_use_parts():
    from realtime.passage_review import PassageReviewSurface
    from realtime.quick_review_panel import QuickReviewPanel
    from realtime.review_window import FinishReviewWindow

    for window, count in ((QuickReviewPanel, 7), (PassageReviewSurface, 7), (FinishReviewWindow, 6)):
        assert len(window._ui_parts) == count
        assert all(issubclass(part, UiPart) for part in window._ui_parts)
        assert not any(issubclass(window, part) for part in window._ui_parts)
