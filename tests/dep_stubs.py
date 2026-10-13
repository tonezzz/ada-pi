"""Missing-runtime-dep stand-ins for test environments.

google-genai and fastapi are backend RUNTIME deps (backend/requirements.txt),
not guaranteed in the bare interpreter that runs `pytest tests/` in gate/CI
worktrees — `from google import genai` and `from fastapi import HTTPException`
used to blow up at collection of the first test module needing them
(test_calendar_providers.py / test_cms_spawn.py), holding the merge gate.

install() is called from tests/conftest.py BEFORE test modules import. For
each dep it is a no-op when the real package is importable; otherwise it
registers minimal duck-typed modules in sys.modules:

- types.<Name> resolves to a fabricated kwargs->attrs model class
  (cached, so isinstance() between backend and test holds)
- UPPER_CASE attribute access fabricates enum members (str subclass —
  ==, .name and .value all work)
- FastAPI route decorators register-and-return; app.mount/on_event/
  middleware/exception_handler accepted; responses are kwargs->attrs
- genai.Client / fastapi.Request exist so patch() has attrs to swap

These stubs cover the surface tests exercise — no HTTP serving, no API.
"""
from __future__ import annotations

import sys
import types as _t


class _Member(str):
    """Enum-member stand-in: equals its own name, carries .name/.value."""

    def __new__(cls, name):
        obj = super().__new__(cls, name)
        obj.name = name
        obj.value = name
        return obj


class _ModelMeta(type):
    def __getattr__(cls, name):
        # Fabricate enum members on first access (Behavior.NON_BLOCKING,
        # FunctionResponseScheduling.WHEN_IDLE, ...) — cached on the
        # class so later equality checks hit the same object.
        if name.isupper():
            member = _Member(name)
            setattr(cls, name, member)
            return member
        raise AttributeError(name)


class _Model(metaclass=_ModelMeta):
    """Pydantic-model stand-in: kwargs become attributes; unset fields
    read as None (real models default declared fields to None)."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None

    def model_copy(self, update=None):
        """pydantic BaseModel.model_copy — shallow copy + field updates."""
        import copy
        new = copy.copy(self)
        for k, v in (update or {}).items():
            setattr(new, k, v)
        return new

    def __repr__(self):
        args = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{type(self).__name__}({args})"


# ---------------------------------------------------------------------------
# google.genai
# ---------------------------------------------------------------------------

class Part(_Model):
    @classmethod
    def from_text(cls, text=None, **kw):
        return cls(text=text, **kw)

    @classmethod
    def from_bytes(cls, data=None, mime_type=None, **kw):
        return cls(inline_data=Blob(data=data, mime_type=mime_type), **kw)


class Blob(_Model):
    pass


class Client(_Model):
    """Constructor-tolerant; tests patch this wholesale anyway."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aio = _Model()


def _fabricating_module(name, explicit=None):
    """Module whose __getattr__ fabricates a cached _Model subclass per
    attribute — covers the whole `types.` / `responses.` surface without
    enumerating it."""
    mod = _t.ModuleType(name)
    for k, v in (explicit or {}).items():
        setattr(mod, k, v)

    def _getattr(attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        cls = _ModelMeta(attr, (_Model,), {})
        setattr(mod, attr, cls)
        return cls

    mod.__getattr__ = _getattr
    return mod


def _install_genai():
    try:
        from google import genai  # noqa: F401
        from google.genai import types  # noqa: F401
        return
    except ImportError:
        pass
    if "google.genai" in sys.modules:
        return

    types_mod = _fabricating_module(
        "google.genai.types", {"Part": Part, "Blob": Blob})
    genai_mod = _t.ModuleType("google.genai")
    genai_mod.types = types_mod
    genai_mod.Client = Client

    try:
        import google
    except ImportError:
        google = _t.ModuleType("google")
        google.__path__ = []
        sys.modules["google"] = google

    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    setattr(google, "genai", genai_mod)


# ---------------------------------------------------------------------------
# fastapi / starlette
# ---------------------------------------------------------------------------

class HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class WebSocketDisconnect(Exception):
    def __init__(self, code=1000, reason=None):
        super().__init__(reason or code)
        self.code = code
        self.reason = reason


class Request(_Model):
    pass


class WebSocket(_Model):
    pass


class BackgroundTasks(_Model):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class Response(_Model):
    def __init__(self, content=None, status_code=200, headers=None,
                 media_type=None, background=None, **kw):
        super().__init__(content=content, status_code=status_code,
                         headers=dict(headers or {}), media_type=media_type,
                         background=background, **kw)
        self.raw_headers = []

    def set_cookie(self, key, value="", **kw):
        self.headers.setdefault("set-cookie", []).append((key, value, kw))

    def delete_cookie(self, key, **kw):
        self.set_cookie(key, "", max_age=0, **kw)


class _ResponseMeta(_ModelMeta):
    """Response subclasses also tolerate positional content."""

    def __call__(cls, *args, **kw):
        if args:
            kw.setdefault("content", args[0])
            if len(args) > 1:
                kw.setdefault("status_code", args[1])
        return super().__call__(**kw)


class RedirectResponse(Response, metaclass=_ResponseMeta):
    def __init__(self, url=None, status_code=307, **kw):
        super().__init__(status_code=status_code, **kw)
        self.url = url


def _responses_module(name):
    mod = _fabricating_module(
        name, {"Response": Response, "RedirectResponse": RedirectResponse})
    orig_getattr = mod.__getattr__

    def _getattr(attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        cls = _ResponseMeta(attr, (Response,), {})
        setattr(mod, attr, cls)
        return cls

    mod.__getattr__ = _getattr
    return mod


class StaticFiles(_Model):
    def __init__(self, directory=None, html=False, packages=None,
                 check_dir=False, **kw):
        super().__init__(directory=directory, html=html,
                         packages=packages, check_dir=check_dir, **kw)


def _decorator(fn):
    return fn


class FastAPI(_Model):
    """Route/middleware decorators register-and-return the handler — the
    tests call the coroutines directly, no dispatch needed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.routes = []
        self.state = _Model()

    def _route(self, method, path, **kw):
        def deco(fn):
            self.routes.append((method, path, fn))
            return fn
        return deco

    def get(self, path, **kw): return self._route("GET", path, **kw)
    def post(self, path, **kw): return self._route("POST", path, **kw)
    def put(self, path, **kw): return self._route("PUT", path, **kw)
    def delete(self, path, **kw): return self._route("DELETE", path, **kw)
    def patch(self, path, **kw): return self._route("PATCH", path, **kw)
    def options(self, path, **kw): return self._route("OPTIONS", path, **kw)
    def head(self, path, **kw): return self._route("HEAD", path, **kw)
    def websocket(self, path, **kw): return self._route("WS", path, **kw)
    def route(self, path, **kw): return self._route("ROUTE", path, **kw)

    def on_event(self, event):
        return _decorator

    def middleware(self, kind):
        return _decorator

    def exception_handler(self, exc):
        return _decorator

    def mount(self, path, app, name=None, **kw):
        self.routes.append(("MOUNT", path, app))

    def add_middleware(self, cls, **kw):
        pass

    def add_route(self, path, route, **kw):
        self.routes.append(("ROUTE", path, route))


def _install_fastapi():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass
    if "fastapi" in sys.modules:
        return

    fastapi_mod = _fabricating_module("fastapi", {
        "FastAPI": FastAPI,
        "HTTPException": HTTPException,
        "Request": Request,
        "Response": Response,
        "WebSocket": WebSocket,
        "WebSocketDisconnect": WebSocketDisconnect,
        "BackgroundTasks": BackgroundTasks,
    })
    responses_mod = _responses_module("fastapi.responses")
    static_mod = _t.ModuleType("fastapi.staticfiles")
    static_mod.StaticFiles = StaticFiles
    fastapi_mod.responses = responses_mod
    fastapi_mod.staticfiles = static_mod
    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.responses"] = responses_mod
    sys.modules["fastapi.staticfiles"] = static_mod

    # fastapi re-exports starlette types — give lazy `from starlette.X
    # import Y` paths the same stand-ins if real starlette is absent too.
    try:
        import starlette  # noqa: F401
        return
    except ImportError:
        pass
    starlette_mod = _fabricating_module("starlette", {"Response": Response})
    starlette_mod.__path__ = []
    sys.modules["starlette"] = starlette_mod
    for sub, explicit in (
        ("applications", {"Starlette": FastAPI}),
        ("requests", {"Request": Request}),
        ("responses", None),          # full fabricated surface
        ("routing", {"Route": _ModelMeta("Route", (_Model,), {})}),
        ("websockets", {"WebSocket": WebSocket,
                        "WebSocketDisconnect": WebSocketDisconnect}),
        ("testclient", None),
        ("middleware", None),
        ("datastructures", None),
        ("exceptions", {"HTTPException": HTTPException}),
    ):
        sub_mod = (_responses_module(f"starlette.{sub}") if sub == "responses"
                   else _fabricating_module(f"starlette.{sub}", explicit))
        sys.modules[f"starlette.{sub}"] = sub_mod
        setattr(starlette_mod, sub, sub_mod)


def install():
    """Register stub modules for each absent runtime dep."""
    _install_genai()
    _install_fastapi()
