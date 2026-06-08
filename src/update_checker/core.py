"""Core implementation of the update_checker package."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import re
import string
import sys
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from enum import Enum, auto
from functools import wraps
from http import HTTPStatus
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import quote

try:
    import aiohttp
except ImportError:  # aiohttp is only required for async support
    aiohttp = None

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from http.client import HTTPResponse

__version__ = version("update_checker")


class _Sentinel(Enum):
    """Distinct sentinel type so a cache miss narrows away from a result."""

    CACHE_MISS = auto()


class _SerializedResult(TypedDict):
    """The JSON-serializable form of an UpdateResult in the permacache."""

    available: str
    package: str
    release_date: str | None
    running: str


CACHE_MISS = _Sentinel.CACHE_MISS
CHUNK_SIZE = 65536
# COMPONENT_RE and REPLACE support parse_version near the bottom of this module
COMPONENT_RE = re.compile(r"(\d+ | [a-z]+ | \.| -)", re.VERBOSE)
DAYS_PER_WEEK = 7
# A runaway guard against memory exhaustion. This bounds the decompressed
# response, so it must stay above the largest real PyPI JSON (a few MiB);
# gzip shrinks the transfer but not the parsed payload this limit governs.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REPLACE = {"-": "final-", "dev": "@", "pre": "c", "preview": "c", "rc": "c"}.get
SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60
TIMEOUT_SECONDS = 1
USER_AGENT = f"update_checker/{__version__}"


class _Cache:
    """In-memory cache of check results backed by a JSON permacache."""

    def __init__(self) -> None:
        """Initialize a _Cache instance."""
        self.expire_time = SECONDS_PER_HOUR
        self.filename: pathlib.Path | None = None
        self.initialized = False
        self.results: dict[tuple[str, ...], tuple[float, UpdateResult | None]] = {}

    def initialize(self) -> None:
        """Determine the permacache location and load it on first use."""
        self.initialized = True
        try:
            directory = _cache_directory()
            directory.mkdir(exist_ok=True, parents=True)
            self.filename = directory / "cache.json"
        except OSError:
            return  # Operate without a permacache
        self.update_from_permacache()

    def retrieve(self, key: tuple[str, str], /) -> UpdateResult | _Sentinel | None:
        """Return the fresh cached result for key, or the CACHE_MISS sentinel.

        Returns:
            The cached result when present and fresh, otherwise CACHE_MISS.

        """
        if not self.initialized:
            self.initialize()
        if key in self.results:
            cache_time, result = self.results[key]
            if time.time() - cache_time < self.expire_time:
                return result
        return CACHE_MISS

    def save_to_permacache(self) -> None:
        """Save the in-memory cache data to the permacache.

        There is a race condition here between two processes updating at the
        same time. It's perfectly acceptable to lose and/or corrupt the
        permacache information as each process's in-memory cache will remain
        in-tact.

        """
        filename = self.filename
        if filename is None:
            return
        self.update_from_permacache()
        data = {
            json.dumps(key): [cache_time, _serialize_result(result)]
            for key, (cache_time, result) in self.results.items()
        }
        try:
            with filename.open("w") as fp:
                json.dump(data, fp)
            # Keep the cache private; it reveals which packages the user runs
            filename.chmod(0o600)
        except OSError:
            pass  # Ignore permacache saving exceptions

    def store(self, *, key: tuple[str, str], value: UpdateResult | None) -> None:
        """Record the result for key and persist it to the permacache."""
        if not self.initialized:
            self.initialize()
        self.results[key] = (time.time(), value)
        if self.filename:
            self.save_to_permacache()

    def update_from_permacache(self) -> None:
        """Attempt to update newer items from the permacache."""
        filename = self.filename
        if filename is None:
            return
        try:
            with filename.open() as fp:
                permacache = json.load(fp)
        except (OSError, ValueError):
            return  # It's okay if it cannot load
        try:
            for raw_key, (cache_time, result) in permacache.items():
                # A non-numeric cache_time would later crash retrieve, so skip
                # any entry that is not a real timestamp
                if not isinstance(cache_time, (int, float)):
                    continue
                key = tuple(json.loads(raw_key))
                if key not in self.results or cache_time > self.results[key][0]:
                    self.results[key] = (cache_time, _deserialize_result(result))
        except (AttributeError, KeyError, TypeError, ValueError):
            pass  # It's okay to ignore malformed permacache data


class UpdateChecker:
    """A class to check for package updates."""

    def __init__(self, *, bypass_cache: bool = False) -> None:
        """Initialize an UpdateChecker instance."""
        self._bypass_cache = bypass_cache

    async def async_check(
        self,
        *,
        package_name: str,
        package_version: str,
    ) -> UpdateResult | None:
        """Return a UpdateResult object if there is a newer version.

        Returns:
            An UpdateResult instance when a newer version exists, otherwise
            None.

        """
        return await _async_check(
            bypass_cache=self._bypass_cache,
            package_name=package_name,
            package_version=package_version,
        )

    def check(
        self,
        *,
        package_name: str,
        package_version: str,
    ) -> UpdateResult | None:
        """Return a UpdateResult object if there is a newer version.

        Returns:
            An UpdateResult instance when a newer version exists, otherwise
            None.

        """
        return _check(
            bypass_cache=self._bypass_cache,
            package_name=package_name,
            package_version=package_version,
        )


class UpdateResult:
    """Contains the information for a package that has an update."""

    def __init__(
        self,
        *,
        available: str,
        package: str,
        release_date: str | None,
        running: str,
    ) -> None:
        """Initialize an UpdateResult instance."""
        # Strip non-printable characters, e.g., terminal escape sequences,
        # from the network-provided version
        self.available_version = re.sub(r"[^ -~]", "", available)
        self.package_name = package
        self.running_version = running
        self.release_date = None
        if release_date:
            # Treat malformed release dates as missing
            with contextlib.suppress(TypeError, ValueError):
                self.release_date = datetime.strptime(
                    release_date,
                    "%Y-%m-%dT%H:%M:%S",
                ).replace(tzinfo=timezone.utc)

    def __str__(self) -> str:
        """Return a printable UpdateResult string.

        Returns:
            A sentence describing the outdated package and newer version.

        """
        message = (
            f"Version {self.running_version} of {self.package_name} is outdated. "
            f"Version {self.available_version} "
        )
        if self.release_date:
            message += f"was released {pretty_date(self.release_date)}."
        else:
            message += "is available."
        return message


def async_cache_results(
    function: Callable[..., Awaitable[UpdateResult | None]],
    /,
) -> Callable[..., Awaitable[UpdateResult | None]]:
    """Return decorated coroutine function that caches the results.

    Returns:
        The decorated coroutine function.

    """
    cache = _Cache()

    @wraps(function)
    async def wrapped(
        *,
        bypass_cache: bool = False,
        package_name: str,
        package_version: str,
        **extra_data: object,
    ) -> UpdateResult | None:
        """Return cached results if available.

        Returns:
            The cached result when fresh, otherwise the live result.

        """
        key = (package_name, package_version)
        if not bypass_cache:
            result = cache.retrieve(key)
            if result is not CACHE_MISS:
                return result
        result = await function(
            package_name=package_name,
            package_version=package_version,
            **extra_data,
        )
        cache.store(key=key, value=result)
        return result

    return wrapped


@async_cache_results
async def _async_check(
    *,
    package_name: str,
    package_version: str,
) -> UpdateResult | None:
    data = await async_query_pypi(
        include_prereleases=not standard_release(package_version),
        package=package_name,
    )
    return _result_from_data(
        data=data,
        package_name=package_name,
        package_version=package_version,
    )


async def async_query_pypi(
    *,
    include_prereleases: bool,
    package: str,
) -> dict[str, Any]:
    """Return information about the current version of package.

    Returns:
        A dict with a "success" key. On success, a "data" key maps to a dict
        with "version" and "upload_time" keys.

    Raises:
        ImportError: When aiohttp is not installed.

    """
    if aiohttp is None:
        msg = 'aiohttp is required for async support: uv add "update_checker[async]"'
        raise ImportError(msg)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    try:
        async with (
            aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            ) as session,
            session.get(
                f"https://pypi.org/pypi/{quote(package, safe='')}/json",
            ) as response,
        ):
            if response.status != HTTPStatus.OK:
                return {"success": False}
            # Cap the body so a hostile response cannot exhaust memory; the
            # total timeout above already bounds how long reading may take
            raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"success": False}
            json_data = json.loads(raw)
    # TimeoutError and asyncio.TimeoutError are distinct prior to Python 3.11
    except (TimeoutError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
        return {"success": False}
    try:
        data = _extract_version(
            include_prereleases=include_prereleases,
            releases=json_data["releases"],
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return {"success": False}  # Ignore malformed responses

    return {"success": True, "data": data}


async def async_update_check(
    *,
    bypass_cache: bool = False,
    package_name: str,
    package_version: str,
) -> None:
    """Output to stderr if an update to the package is available."""
    checker = UpdateChecker(bypass_cache=bypass_cache)
    result = await checker.async_check(
        package_name=package_name,
        package_version=package_version,
    )
    if result:
        sys.stderr.write(f"{_colorize(str(result))}\n")


def _cache_directory() -> pathlib.Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return pathlib.Path(base).expanduser() / "update_checker"


def cache_results(
    function: Callable[..., UpdateResult | None],
    /,
) -> Callable[..., UpdateResult | None]:
    """Return decorated function that caches the results.

    Returns:
        The decorated function.

    """
    cache = _Cache()

    @wraps(function)
    def wrapped(
        *,
        bypass_cache: bool = False,
        package_name: str,
        package_version: str,
        **extra_data: object,
    ) -> UpdateResult | None:
        """Return cached results if available.

        Returns:
            The cached result when fresh, otherwise the live result.

        """
        key = (package_name, package_version)
        if not bypass_cache:
            result = cache.retrieve(key)
            if result is not CACHE_MISS:
                return result
        result = function(
            package_name=package_name,
            package_version=package_version,
            **extra_data,
        )
        cache.store(key=key, value=result)
        return result

    return wrapped


@cache_results
def _check(*, package_name: str, package_version: str) -> UpdateResult | None:
    data = query_pypi(
        include_prereleases=not standard_release(package_version),
        package=package_name,
    )
    return _result_from_data(
        data=data,
        package_name=package_name,
        package_version=package_version,
    )


def _colorize(text: str, /) -> str:
    """Return text wrapped in ANSI yellow when stderr supports color.

    Color is emitted when stderr is a terminal, honoring the NO_COLOR and
    FORCE_COLOR conventions (https://no-color.org, https://force-color.org).

    Returns:
        The text, wrapped in ANSI yellow escape codes when appropriate.

    """
    if "NO_COLOR" in os.environ:
        return text
    if "FORCE_COLOR" not in os.environ and not (
        hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    ):
        return text
    return f"\033[33m{text}\033[0m"


def _deserialize_result(data: _SerializedResult | None, /) -> UpdateResult | None:
    if data is None:
        return None
    return UpdateResult(
        available=data["available"],
        package=data["package"],
        release_date=data["release_date"],
        running=data["running"],
    )


def _extract_version(
    *,
    include_prereleases: bool,
    releases: dict[str, list[dict[str, Any]]],
) -> dict[str, str | None]:
    versions = sorted(releases, key=parse_version, reverse=True)
    version = versions[0]
    for tmp_version in versions:
        if include_prereleases or standard_release(tmp_version):
            version = tmp_version
            break

    upload_time = None
    for file_info in releases[version]:
        if file_info["upload_time"]:
            upload_time = file_info["upload_time"]
            break

    return {"upload_time": upload_time, "version": version}


def _gunzip_capped(data: bytes, /) -> bytes:
    # wbits of MAX_WBITS | 16 selects the gzip format
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        result = decompressor.decompress(data, MAX_RESPONSE_BYTES + 1)
    except zlib.error as exception:
        msg = "response could not be decompressed"
        raise ValueError(msg) from exception
    if len(result) > MAX_RESPONSE_BYTES or decompressor.unconsumed_tail:
        msg = "decompressed response exceeded the maximum allowed size"
        raise ValueError(msg)
    return result


# The following two functions are taken from setuptools pkg_resources.py (PSF
# license), along with the COMPONENT_RE and REPLACE constants near the top of
# this module. Unfortunately importing pkg_resources to directly use the
# parse_version function results in some undesired side effects.


def parse_version(s: str, /) -> tuple[str, ...]:
    """Convert a version string to a chronologically-sortable key.

    This is a rough cross between distutils' StrictVersion and LooseVersion;
    if you give it versions that would work with StrictVersion, then it behaves
    the same; otherwise it acts like a slightly-smarter LooseVersion. It is
    *possible* to create pathological version coding schemes that will fool
    this parser, but they should be very rare in practice.

    The returned value will be a tuple of strings.  Numeric portions of the
    version are padded to 8 digits so they will compare numerically, but
    without relying on how numbers compare relative to strings.  Dots are
    dropped, but dashes are retained.  Trailing zeros between alpha segments
    or dashes are suppressed, so that e.g. "2.4.0" is considered the same as
    "2.4". Alphanumeric parts are lower-cased.

    The algorithm assumes that strings like "-" and any alpha string that
    alphabetically follows "final"  represents a "patch level".  So, "2.4-1"
    is assumed to be a branch or patch of "2.4", and therefore "2.4.1" is
    considered newer than "2.4-1", which in turn is newer than "2.4".

    Strings like "a", "b", "c", "alpha", "beta", "candidate" and so on (that
    come before "final" alphabetically) are assumed to be pre-release versions,
    so that the version "2.4" is considered newer than "2.4a1".

    Finally, to handle miscellaneous cases, the strings "pre", "preview", and
    "rc" are treated as if they were "c", i.e. as though they were release
    candidates, and therefore are not as new as a version string that does not
    contain them, and "dev" is replaced with an '@' so that it sorts lower than
    than any other pre-release tag.

    Returns:
        A chronologically-sortable tuple of strings.

    """
    parts = []
    for part in _parse_version_parts(s.lower()):
        if part.startswith("*"):
            if part < "*final":  # remove '-' before a prerelease tag
                while parts and parts[-1] == "*final-":
                    parts.pop()
            # remove trailing zeros from each series of numeric parts
            while parts and parts[-1] == "00000000":
                parts.pop()
        parts.append(part)
    return tuple(parts)


def _parse_version_parts(s: str, /) -> Iterator[str]:
    for raw_part in COMPONENT_RE.split(s):
        part = REPLACE(raw_part, raw_part)
        if not part or part == ".":
            continue
        if part[:1] in string.digits:
            yield part.zfill(8)  # pad for numeric comparison
        else:
            yield "*" + part

    yield "*final"  # ensure that alpha/beta/candidate are before final


def pretty_date(the_datetime: datetime, /) -> str:
    """Attempt to return a human-readable time delta string.

    Returns:
        A human-readable relative time, e.g., "3 days ago", or the formatted
        date when more than a week old.

    """
    # Source modified from
    # http://stackoverflow.com/a/5164027/176978
    if the_datetime.tzinfo is None:
        # Handle naive datetimes, such as those unpickled from the permacache
        # written by previous versions
        the_datetime = the_datetime.replace(tzinfo=timezone.utc)
    diff = datetime.now(timezone.utc) - the_datetime
    if diff.days > DAYS_PER_WEEK or diff.days < 0:
        return the_datetime.strftime("%A %B %d, %Y")
    if diff.days:
        return "1 day ago" if diff.days == 1 else f"{diff.days} days ago"
    buckets = (
        (2, "just now"),
        (SECONDS_PER_MINUTE, f"{diff.seconds} seconds ago"),
        (2 * SECONDS_PER_MINUTE, "1 minute ago"),
        (SECONDS_PER_HOUR, f"{round(diff.seconds / SECONDS_PER_MINUTE)} minutes ago"),
        (2 * SECONDS_PER_HOUR, "1 hour ago"),
    )
    for threshold, message in buckets:
        if diff.seconds < threshold:
            return message
    return f"{round(diff.seconds / SECONDS_PER_HOUR)} hours ago"


def query_pypi(*, include_prereleases: bool, package: str) -> dict[str, Any]:
    """Return information about the current version of package.

    Returns:
        A dict with a "success" key. On success, a "data" key maps to a dict
        with "version" and "upload_time" keys.

    """
    # build_opener keeps the default handlers (notably proxy support) while
    # letting us request a gzip-compressed response
    opener = urllib.request.build_opener()
    opener.addheaders = [("Accept-Encoding", "gzip"), ("User-Agent", USER_AGENT)]
    url = f"https://pypi.org/pypi/{quote(package, safe='')}/json"
    try:
        # open raises HTTPError, an OSError, for non-2xx responses
        with opener.open(url, timeout=TIMEOUT_SECONDS) as response:
            raw = _read_capped(response)
            encoding = response.headers.get("Content-Encoding")
        raw = _gunzip_capped(raw) if encoding == "gzip" else raw
        json_data = json.loads(raw)
    except (OSError, ValueError):
        return {"success": False}
    try:
        data = _extract_version(
            include_prereleases=include_prereleases,
            releases=json_data["releases"],
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return {"success": False}  # Ignore malformed responses

    return {"success": True, "data": data}


def _read_capped(response: HTTPResponse, /) -> bytes:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(CHUNK_SIZE):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            msg = "response exceeded the maximum allowed size"
            raise ValueError(msg)
        if time.monotonic() > deadline:
            msg = "response exceeded the time budget"
            raise ValueError(msg)
        chunks.append(chunk)
    return b"".join(chunks)


def _result_from_data(
    *,
    data: dict[str, Any],
    package_name: str,
    package_version: str,
) -> UpdateResult | None:
    if not data.get("success") or (
        parse_version(package_version) >= parse_version(data["data"]["version"])
    ):
        return None

    return UpdateResult(
        available=data["data"]["version"],
        package=package_name,
        release_date=data["data"]["upload_time"],
        running=package_version,
    )


def _serialize_result(result: UpdateResult | None, /) -> _SerializedResult | None:
    if result is None:
        return None
    return {
        "available": result.available_version,
        "package": result.package_name,
        "release_date": (
            result.release_date.strftime("%Y-%m-%dT%H:%M:%S")
            if result.release_date
            else None
        ),
        "running": result.running_version,
    }


def standard_release(version: str, /) -> bool:
    """Return whether version is a release that is not a pre-release.

    Returns:
        True when version contains only dot-separated digits.

    """
    return version.replace(".", "").isdigit()


def update_check(
    package_name: str,
    package_version: str,
    *,
    bypass_cache: bool = False,
) -> None:
    """Output to stderr if an update to the package is available."""
    checker = UpdateChecker(bypass_cache=bypass_cache)
    result = checker.check(
        package_name=package_name,
        package_version=package_version,
    )
    if result:
        sys.stderr.write(f"{_colorize(str(result))}\n")
