"""Tests for the error hierarchy — and for how server.py turns it into HTTP.

The previous version of this file asserted each class's own attribute values
(``ConfigError().status_code == 400``) across 15 tests. That restated the
declaration rather than exercising it: it could only fail if someone edited
errors.py, and it gave the module 100% coverage while every class in it went
unraised in production. The mapping the global handler performs is what callers
actually depend on, so that is what is asserted here.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from rwmod.errors import (
    ConfigError,
    ConflictError,
    DownloadError,
    ModNotFoundError,
    RwmodError,
    SteamCmdError,
    ValidationError,
    WorkshopError,
)

# each error class → the HTTP status the global handler must produce for it
STATUS_MAP = [
    (ConfigError, 400),
    (ValidationError, 400),
    (ModNotFoundError, 404),
    (ConflictError, 409),
    (RwmodError, 500),
    (SteamCmdError, 502),
    (WorkshopError, 502),
    (DownloadError, 502),
]


class TestRwmodError:
    def test_explicit_detail_overrides_the_class_default(self):
        assert RwmodError("自定义错误消息").detail == "自定义错误消息"

    def test_class_default_detail_applies_when_omitted(self):
        assert ConfigError().detail == "配置错误"

    def test_str_is_the_detail(self):
        assert str(ModNotFoundError("Mod 1234567890 不存在")) == "Mod 1234567890 不存在"

    def test_every_subclass_is_catchable_as_rwmod_error(self):
        for cls in (ConfigError, SteamCmdError, WorkshopError, ModNotFoundError):
            assert issubclass(cls, RwmodError)
            assert isinstance(cls("test"), Exception)


@pytest.mark.parametrize(("exc_cls", "expected_status"), STATUS_MAP)
def test_handler_maps_each_error_to_its_status(exc_cls, expected_status):
    """A router raising an errors.py subclass is only half the contract; this
    covers the other half — the status and body shape the client receives."""
    from rwmod.server import rwmod_error_handler

    request = MagicMock()
    request.method = "GET"
    request.url.path = "/api/whatever"

    response = asyncio.run(rwmod_error_handler(request, exc_cls("具体原因")))

    assert response.status_code == expected_status
    assert json.loads(response.body) == {"error": exc_cls.__name__, "detail": "具体原因"}
