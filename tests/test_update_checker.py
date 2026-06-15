"""Tests for the update_checker package."""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING
from unittest import mock

import aiohttp

from update_checker import (
    UpdateChecker,
    UpdateResult,
    async_update_check,
    pretty_date,
    update_check,
)
from update_checker.core import (
    CACHE_MISS,
    USER_AGENT,
    _Cache,
    _colorize,
    _deserialize_result,
    _serialize_result,
    query_pypi,
)

if TYPE_CHECKING:
    import pathlib
    from typing import Self

    import pytest

PACKAGE = "praw"


class FakeContent:
    """Stand-in for the StreamReader exposed by an aiohttp response."""

    def __init__(self, body: bytes, /) -> None:
        """Initialize a FakeContent instance."""
        self._body = body

    async def read(self, size: int = -1, /) -> bytes:
        """Return up to size bytes of the canned body.

        Returns:
            The first size bytes of the body, or all of it when size is
            negative.

        """
        return self._body if size < 0 else self._body[:size]


class FakeResponse:
    """Async context manager standing in for an aiohttp response."""

    async def __aenter__(self) -> Self:
        """Return the response.

        Returns:
            The response itself.

        """
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Do nothing."""

    def __init__(
        self,
        *,
        body: bytes | None = None,
        json_data: object = None,
        status: int = 200,
    ) -> None:
        """Initialize a FakeResponse instance."""
        if body is None:
            body = b"" if json_data is None else json.dumps(json_data).encode()
        self.content = FakeContent(body)
        self.status = status


class FakeSession:
    """Async context manager standing in for an aiohttp client session."""

    async def __aenter__(self) -> Self:
        """Return the session.

        Returns:
            The session itself.

        """
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Do nothing."""

    def __init__(
        self,
        response: FakeResponse | Exception,
        /,
        **_kwargs: object,
    ) -> None:
        """Initialize a FakeSession instance."""
        self._response = response

    def get(self, _url: str, /) -> FakeResponse:
        """Return the canned response, or raise the canned exception.

        Returns:
            The canned response.

        """
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeSyncResponse(io.BytesIO):
    """Context manager standing in for an HTTPResponse from urlopen."""

    def __init__(self, body: bytes, /, *, content_encoding: str | None = None) -> None:
        """Initialize a FakeSyncResponse instance."""
        super().__init__(body)
        self.headers = (
            {"Content-Encoding": content_encoding} if content_encoding else {}
        )


def fake_async_pypi(response: FakeResponse | Exception, /) -> mock._patch:
    return mock.patch("aiohttp.ClientSession", partial(FakeSession, response))


def fake_sync_pypi(
    response: bytes | Exception | object,
    /,
    *,
    content_encoding: str | None = None,
) -> mock._patch:
    target = "urllib.request.OpenerDirector.open"
    if isinstance(response, Exception):
        return mock.patch(target, side_effect=response)
    body = response if isinstance(response, bytes) else json.dumps(response).encode()
    return mock.patch(
        target,
        return_value=FakeSyncResponse(body, content_encoding=content_encoding),
    )


async def test_async_checker_check__malformed_json() -> None:
    with fake_async_pypi(FakeResponse(body=b"not json")):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_checker_check__missing_releases() -> None:
    with fake_async_pypi(FakeResponse(json_data={})):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_checker_check__oversized_response() -> None:
    response = FakeResponse(json_data={"releases": {"0.0.1": [], "5.0.0": []}})
    with (
        mock.patch("update_checker.core.MAX_RESPONSE_BYTES", 4),
        fake_async_pypi(response),
    ):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_checker_check__status_error() -> None:
    with fake_async_pypi(FakeResponse(status=503)):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_checker_check__successful() -> None:
    response = FakeResponse(json_data={"releases": {"0.0.1": [], "5.0.0": []}})
    with fake_async_pypi(response):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result.available_version == "5.0.0"


async def test_async_checker_check__timeout() -> None:
    with fake_async_pypi(TimeoutError()):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_checker_check__unsuccessful() -> None:
    with fake_async_pypi(aiohttp.ClientError()):
        checker = UpdateChecker(bypass_cache=True)
        result = await checker.async_check(
            package_name=PACKAGE,
            package_version="1.0.0",
        )
    assert result is None


async def test_async_update_check__forced_color(
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = FakeResponse(json_data={"releases": {"0.0.1": [], "5.0.0": []}})
    with (
        mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True),
        fake_async_pypi(response),
    ):
        await async_update_check(
            bypass_cache=True,
            package_name=PACKAGE,
            package_version="0.0.1",
        )
    assert capsys.readouterr().err.startswith("\033[33m")


async def test_async_update_check__successful__has_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = FakeResponse(json_data={"releases": {"0.0.1": [], "5.0.0": []}})
    with fake_async_pypi(response):
        await async_update_check(
            bypass_cache=True,
            package_name=PACKAGE,
            package_version="0.0.1",
        )
    assert (
        capsys.readouterr().err
        == "Version 0.0.1 of praw is outdated. Version 5.0.0 is available.\n"
    )


async def test_async_update_check__unsuccessful(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with fake_async_pypi(aiohttp.ClientError()):
        await async_update_check(
            bypass_cache=True,
            package_name=PACKAGE,
            package_version="0.0.1",
        )
    assert not capsys.readouterr().err


def test_cache_permacache__ignores_non_numeric_timestamp(
    tmp_path: pathlib.Path,
) -> None:
    key = (PACKAGE, "1.0")
    filename = tmp_path / "cache.json"
    filename.write_text(json.dumps({json.dumps(key): ["whenever", None]}))

    cache = _Cache()
    cache.filename = filename
    cache.initialized = True
    cache.update_from_permacache()
    assert key not in cache.results
    # retrieve must not raise on a poisoned permacache
    assert cache.retrieve(key) is CACHE_MISS


def test_cache_permacache__round_trips_keys_with_special_characters(
    tmp_path: pathlib.Path,
) -> None:
    key = ("name|with|pipes", "1.0")
    writer = _Cache()
    writer.filename = tmp_path / "cache.json"
    writer.initialized = True
    writer.store(key=key, value=None)

    reader = _Cache()
    reader.filename = tmp_path / "cache.json"
    reader.update_from_permacache()
    assert key in reader.results


def test_cache_permacache__written_with_owner_only_permissions(
    tmp_path: pathlib.Path,
) -> None:
    if sys.platform == "win32":
        return  # POSIX permission bits are not meaningful on Windows
    cache = _Cache()
    cache.filename = tmp_path / "cache.json"
    cache.initialized = True
    cache.store(key=(PACKAGE, "1.0"), value=None)
    assert cache.filename.stat().st_mode & 0o777 == 0o600


def test_checker_check__gzip_encoded_response() -> None:
    body = gzip.compress(json.dumps({"releases": {"0.0.1": [], "5.0.0": []}}).encode())
    with fake_sync_pypi(body, content_encoding="gzip"):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result.available_version == "5.0.0"


def test_checker_check__malformed_json() -> None:
    with fake_sync_pypi(b"not json"):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__missing_releases() -> None:
    with fake_sync_pypi({}):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__no_update_to_beta_version() -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "3.7.0b1": []}}):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="3.6")
    assert result is None


def test_checker_check__oversized_decompressed_response() -> None:
    body = gzip.compress(json.dumps({"releases": {"x" * 10000: []}}).encode())
    with (
        mock.patch("update_checker.core.MAX_RESPONSE_BYTES", len(body) + 100),
        fake_sync_pypi(body, content_encoding="gzip"),
    ):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__oversized_response() -> None:
    with (
        mock.patch("update_checker.core.MAX_RESPONSE_BYTES", 4),
        fake_sync_pypi({"releases": {"0.0.1": [], "5.0.0": []}}),
    ):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__status_error() -> None:
    error = urllib.error.HTTPError("url", 503, "unavailable", None, None)
    with fake_sync_pypi(error):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__successful() -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "5.0.0": []}}):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result.available_version == "5.0.0"


def test_checker_check__unsuccessful() -> None:
    with fake_sync_pypi(urllib.error.URLError("connection refused")):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result is None


def test_checker_check__update_to_beta_version_from_beta_version() -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "4.0.0b5": []}}):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="4.0.0b4")
    assert result.available_version == "4.0.0b5"


def test_checker_check__update_to_rc_version_from_beta_version() -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "4.0.0rc1": []}}):
        checker = UpdateChecker(bypass_cache=True)
        result = checker.check(package_name=PACKAGE, package_version="4.0.0b4")
    assert result.available_version == "4.0.0rc1"


def test_colorize__forced_on() -> None:
    with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
        assert _colorize("hi") == "\033[33mhi\033[0m"


def test_colorize__no_color_wins_over_force_color() -> None:
    env = {"FORCE_COLOR": "1", "NO_COLOR": "1"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert _colorize("hi") == "hi"


def test_colorize__non_tty_is_plain() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        # capsys replaces stderr with a non-tty buffer
        assert _colorize("hi") == "hi"


def test_pretty_date__aware_datetime() -> None:
    assert pretty_date(datetime.now(timezone.utc) - timedelta(days=3)) == "3 days ago"


def test_pretty_date__just_now() -> None:
    assert pretty_date(datetime.now(timezone.utc)) == "just now"


def test_pretty_date__naive_datetime() -> None:
    # Naive datetimes, such as those unpickled from permacaches written by
    # previous versions, are interpreted as UTC
    naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert pretty_date(naive_utc - timedelta(days=3)) == "3 days ago"


def test_query_pypi__quotes_package_name() -> None:
    with fake_sync_pypi({}) as opener_open:
        query_pypi(include_prereleases=False, package="a/b c")
    assert opener_open.call_args.args[0] == "https://pypi.org/pypi/a%2Fb%20c/json"


def test_query_pypi__sets_user_agent() -> None:
    captured: dict[str, list[tuple[str, str]]] = {}

    def fake_open(opener: object, _url: str, /, **_kwargs: object) -> FakeSyncResponse:
        captured["headers"] = opener.addheaders
        return FakeSyncResponse(b"{}")

    with mock.patch(
        "urllib.request.OpenerDirector.open",
        autospec=True,
        side_effect=fake_open,
    ):
        query_pypi(include_prereleases=False, package=PACKAGE)
    assert ("User-Agent", USER_AGENT) in captured["headers"]


def test_serialize_result__round_trip() -> None:
    result = UpdateResult(
        available="2.0",
        package=PACKAGE,
        release_date="2026-06-01T12:00:00",
        running="1.0",
    )
    restored = _deserialize_result(_serialize_result(result))
    assert restored.available_version == result.available_version
    assert restored.package_name == result.package_name
    assert restored.release_date == result.release_date
    assert restored.running_version == result.running_version


def test_serialize_result__round_trip_none() -> None:
    assert _deserialize_result(_serialize_result(None)) is None


def test_update_check__forced_color(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True),
        fake_sync_pypi({"releases": {"0.0.1": [], "5.0.0": []}}),
    ):
        update_check(PACKAGE, "0.0.1", bypass_cache=True)
    assert capsys.readouterr().err.startswith("\033[33m")


def test_update_check__successful__has_no_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "0.0.2": []}}):
        update_check(PACKAGE, "0.0.2", bypass_cache=True)
    assert not capsys.readouterr().err


def test_update_check__successful__has_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with fake_sync_pypi({"releases": {"0.0.1": [], "5.0.0": []}}):
        update_check(PACKAGE, "0.0.1", bypass_cache=True)
    assert (
        capsys.readouterr().err
        == "Version 0.0.1 of praw is outdated. Version 5.0.0 is available.\n"
    )


def test_update_check__unsuccessful(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with fake_sync_pypi(urllib.error.URLError("connection refused")):
        update_check(PACKAGE, "0.0.1", bypass_cache=True)
    assert not capsys.readouterr().err


def test_update_result__malformed_release_date() -> None:
    result = UpdateResult(
        available="2.0",
        package=PACKAGE,
        release_date="not a date",
        running="1.0",
    )
    assert result.release_date is None
    assert str(result).endswith("is available.")


def test_update_result__release_date_is_timezone_aware() -> None:
    result = UpdateResult(
        available="2.0",
        package=PACKAGE,
        release_date="2026-06-01T12:00:00",
        running="1.0",
    )
    assert result.release_date.tzinfo == timezone.utc


def test_update_result__sanitizes_available_version() -> None:
    result = UpdateResult(
        available="2.0\x1b[31mhax\x07",
        package=PACKAGE,
        release_date=None,
        running="1.0",
    )
    assert result.available_version == "2.0[31mhax"


def test_update_result__str_with_release_date() -> None:
    release_date = datetime.now(timezone.utc) - timedelta(days=3)
    result = UpdateResult(
        available="2.0",
        package=PACKAGE,
        release_date=release_date.strftime("%Y-%m-%dT%H:%M:%S"),
        running="1.0",
    )
    assert (
        str(result) == "Version 1.0 of praw is outdated. "
        "Version 2.0 was released 3 days ago."
    )
