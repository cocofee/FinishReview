"""Split a large window class into parts without changing how it behaves.

A part holds the code of one area of a window (recording, wave review,
filmstrip, ...).  The window keeps every widget and every piece of state;
inside a part the window is ``self._host``.  ``install_parts`` gives the
window one forwarding method for each part method, so signal connections,
subclass overrides and test doubles keep working exactly as when the code
lived in the window class itself.

Two rules keep the window safe on site:

* A forwarder has exactly the same parameters as the part method.  PyQt
  drops surplus signal arguments (``clicked`` passes ``checked``) only when
  the call itself is rejected, which would not happen with ``*args``.
* A part object is made for each call and only points at the window, so
  the window and its parts never form a reference cycle.  Qt widgets are
  therefore still released at once when closed, never later by the garbage
  collector on another thread.

``install_parts`` refuses a name defined both in the window and in a part,
or in two parts of the same window, so code can no longer be silently
shadowed.  Overriding a method of a base class stays allowed.
"""

from __future__ import annotations

import inspect
from typing import Any, Iterable

_PART = "_ui_part_class_"
_FUNC = "_ui_part_function_"


class UiPart:
    """Code of one area of a window; all state stays on ``self._host``."""

    __slots__ = ("_host",)

    def __init__(self, host: Any) -> None:
        self._host = host


def _parameter_lists(function) -> tuple[str, str]:
    code = function.__code__
    names = code.co_varnames
    positional = list(names[: code.co_argcount])
    keyword_only = list(names[code.co_argcount : code.co_argcount + code.co_kwonlyargcount])
    index = code.co_argcount + code.co_kwonlyargcount
    var_positional = var_keyword = None
    if code.co_flags & inspect.CO_VARARGS:
        var_positional = names[index]
        index += 1
    if code.co_flags & inspect.CO_VARKEYWORDS:
        var_keyword = names[index]
    if {_PART, _FUNC} & set(names):
        raise TypeError(f"{function.__qualname__} uses a reserved parameter name")

    defaults = len(function.__defaults__ or ())
    keyword_defaults = function.__kwdefaults__ or {}
    header: list[str] = []
    call: list[str] = []
    for position, name in enumerate(positional):
        has_default = position >= len(positional) - defaults
        header.append(f"{name}=None" if has_default else name)
        if position == code.co_posonlyargcount - 1:
            header.append("/")
        call.append(f"{_PART}({name})" if position == 0 else name)
    if var_positional:
        header.append(f"*{var_positional}")
        call.append(f"*{var_positional}")
    elif keyword_only:
        header.append("*")
    for name in keyword_only:
        header.append(f"{name}=None" if name in keyword_defaults else name)
        call.append(f"{name}={name}")
    if var_keyword:
        header.append(f"**{var_keyword}")
        call.append(f"**{var_keyword}")
    return ", ".join(header), ", ".join(call)


def _forward_function(host_class: type, part: type, name: str, function):
    if not function.__code__.co_argcount:
        raise TypeError(f"{part.__name__}.{name} takes no self")
    header, call = _parameter_lists(function)
    source = f"def {name}({header}):\n    return {_FUNC}({call})\n"
    namespace: dict[str, Any] = {_PART: part, _FUNC: function}
    exec(compile(source, f"<{part.__name__}.{name} forwarder>", "exec"), namespace)
    forwarder = namespace[name]
    forwarder.__defaults__ = function.__defaults__
    forwarder.__kwdefaults__ = function.__kwdefaults__
    forwarder.__module__ = function.__module__
    forwarder.__qualname__ = f"{host_class.__qualname__}.{name}"
    forwarder.__doc__ = function.__doc__
    forwarder.__dict__.update(function.__dict__)
    forwarder.__wrapped__ = function
    return forwarder


def _forward_property(host_class: type, part: type, name: str, value: property) -> property:
    accessors = [
        None if accessor is None else _forward_function(host_class, part, name, accessor)
        for accessor in (value.fget, value.fset, value.fdel)
    ]
    return property(*accessors, doc=value.__doc__)


def _forward(host_class: type, part: type, name: str, value: Any) -> Any:
    if inspect.isfunction(value):
        return _forward_function(host_class, part, name, value)
    if isinstance(value, property):
        return _forward_property(host_class, part, name, value)
    # Static and class methods and constants never see the part; the window
    # gets the very same object.
    return value


def install_parts(host_class: type, parts: Iterable[type]) -> None:
    """Give ``host_class`` a forwarder for every name its parts define."""

    parts = tuple(parts)
    owners: dict[str, type] = {}
    for part in parts:
        if not issubclass(part, UiPart):
            raise TypeError(f"{part.__name__} is not a UiPart")
        for name, value in vars(part).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in vars(host_class):
                raise TypeError(
                    f"{host_class.__name__}.{name} is defined both in the window and in {part.__name__}"
                )
            if name in owners:
                raise TypeError(
                    f"{host_class.__name__}.{name} is defined in both {owners[name].__name__} and {part.__name__}"
                )
            owners[name] = part
    for name, part in owners.items():
        setattr(host_class, name, _forward(host_class, part, name, vars(part)[name]))
    host_class._ui_parts = parts


__all__ = ["UiPart", "install_parts"]
