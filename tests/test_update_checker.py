"""Tests for the update_checker package."""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from update_checker import UpdateChecker, UpdateResult, pretty_date, update_check
from update_checker.core import _deserialize_result, _serialize_result

PACKAGE = "praw"


def mock_response(*, latest_version: str = "5.0.0", response: mock.MagicMock) -> None:
    response.json = mock.Mock(
        return_value={"releases": {"0.0.1": [], latest_version: []}},
    )
    response.status_code = 200


@mock.patch("requests.get")
def test_checker_check__malformed_json(mock_get: mock.MagicMock) -> None:
    mock_get.return_value.json = mock.Mock(side_effect=ValueError)
    mock_get.return_value.status_code = 200
    checker = UpdateChecker(bypass_cache=True)
    assert checker.check(package_name=PACKAGE, package_version="1.0.0") is None


@mock.patch("requests.get")
def test_checker_check__missing_releases(mock_get: mock.MagicMock) -> None:
    mock_get.return_value.json = mock.Mock(return_value={})
    mock_get.return_value.status_code = 200
    checker = UpdateChecker(bypass_cache=True)
    assert checker.check(package_name=PACKAGE, package_version="1.0.0") is None


@mock.patch("requests.get")
def test_checker_check__no_update_to_beta_version(mock_get: mock.MagicMock) -> None:
    mock_response(latest_version="3.7.0b1", response=mock_get.return_value)
    checker = UpdateChecker(bypass_cache=True)
    assert checker.check(package_name=PACKAGE, package_version="3.6") is None


@mock.patch("requests.get")
def test_checker_check__successful(mock_get: mock.MagicMock) -> None:
    mock_response(response=mock_get.return_value)
    checker = UpdateChecker(bypass_cache=True)
    result = checker.check(package_name=PACKAGE, package_version="1.0.0")
    assert result.available_version == "5.0.0"


@mock.patch("requests.get")
def test_checker_check__unsuccessful(mock_get: mock.MagicMock) -> None:
    mock_get.side_effect = requests.exceptions.RequestException
    checker = UpdateChecker(bypass_cache=True)
    assert checker.check(package_name=PACKAGE, package_version="1.0.0") is None


@mock.patch("requests.get")
def test_checker_check__update_to_beta_version_from_beta_version(
    mock_get: mock.MagicMock,
) -> None:
    mock_response(latest_version="4.0.0b5", response=mock_get.return_value)
    checker = UpdateChecker(bypass_cache=True)
    result = checker.check(package_name=PACKAGE, package_version="4.0.0b4")
    assert result.available_version == "4.0.0b5"


@mock.patch("requests.get")
def test_checker_check__update_to_rc_version_from_beta_version(
    mock_get: mock.MagicMock,
) -> None:
    mock_response(latest_version="4.0.0rc1", response=mock_get.return_value)
    checker = UpdateChecker(bypass_cache=True)
    result = checker.check(package_name=PACKAGE, package_version="4.0.0b4")
    assert result.available_version == "4.0.0rc1"


def test_pretty_date__aware_datetime() -> None:
    assert pretty_date(datetime.now(timezone.utc) - timedelta(days=3)) == "3 days ago"


def test_pretty_date__just_now() -> None:
    assert pretty_date(datetime.now(timezone.utc)) == "just now"


def test_pretty_date__naive_datetime() -> None:
    # Naive datetimes, such as those unpickled from permacaches written by
    # previous versions, are interpreted as UTC
    naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert pretty_date(naive_utc - timedelta(days=3)) == "3 days ago"


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


@mock.patch("requests.get")
def test_update_check__successful__has_no_update(
    mock_get: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_response(latest_version="0.0.2", response=mock_get.return_value)
    update_check(PACKAGE, "0.0.2", bypass_cache=True)
    assert not capsys.readouterr().err


@mock.patch("requests.get")
def test_update_check__successful__has_update(
    mock_get: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_response(response=mock_get.return_value)
    update_check(PACKAGE, "0.0.1", bypass_cache=True)
    assert (
        capsys.readouterr().err
        == "Version 0.0.1 of praw is outdated. Version 5.0.0 is available.\n"
    )


@mock.patch("requests.get")
def test_update_check__unsuccessful(
    mock_get: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_get.side_effect = requests.exceptions.RequestException
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
