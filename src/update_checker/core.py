"""Core implementation of the update_checker package."""

from __future__ import annotations

import pathlib
import pickle  # noqa: S403 -- permacache slated for replacement with JSON
import re
import string
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from http import HTTPStatus
from importlib.metadata import version
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__version__ = version("update_checker")

# COMPONENT_RE and REPLACE support parse_version near the bottom of this module
COMPONENT_RE = re.compile(r"(\d+ | [a-z]+ | \.| -)", re.VERBOSE)
DAYS_PER_WEEK = 7
REPLACE = {"-": "final-", "dev": "@", "pre": "c", "preview": "c", "rc": "c"}.get
SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60


class UpdateChecker:
    """A class to check for package updates."""

    def __init__(self, *, bypass_cache: bool = False) -> None:
        """Initialize an UpdateChecker instance."""
        self._bypass_cache = bypass_cache

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
        self.available_version = available
        self.package_name = package
        self.running_version = running
        if release_date:
            self.release_date = datetime.strptime(
                release_date,
                "%Y-%m-%dT%H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        else:
            self.release_date = None

    def __str__(self) -> str:
        """Return a printable UpdateResult string.

        Returns:
            A sentence describing the outdated package and newer version.

        """
        retval = (
            f"Version {self.running_version} of {self.package_name} is outdated. "
            f"Version {self.available_version} "
        )
        if self.release_date:
            retval += f"was released {pretty_date(self.release_date)}."
        else:
            retval += "is available."
        return retval


def cache_results(  # noqa: C901 -- the nested helpers inflate the count
    function: Callable[..., UpdateResult | None],
    /,
) -> Callable[..., UpdateResult | None]:
    """Return decorated function that caches the results.

    Note: the classes above must be defined before this decorator is applied
    so that loading the permacache at decoration time can unpickle their
    instances.

    Returns:
        The decorated function.

    """

    def save_to_permacache() -> None:
        """Save the in-memory cache data to the permacache.

        There is a race condition here between two processes updating at the
        same time. It's perfectly acceptable to lose and/or corrupt the
        permacache information as each process's in-memory cache will remain
        in-tact.

        """
        update_from_permacache()
        try:
            with filename.open("wb") as fp:
                pickle.dump(cache, fp, pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass  # Ignore permacache saving exceptions

    def update_from_permacache() -> None:
        """Attempt to update newer items from the permacache."""
        try:
            with filename.open("rb") as fp:
                permacache = pickle.load(fp)  # noqa: S301 -- slated for JSON
        except Exception:  # noqa: BLE001 -- unpickling can raise anything
            return  # It's okay if it cannot load
        for key, value in permacache.items():
            if key not in cache or value[0] > cache[key][0]:
                cache[key] = value

    cache = {}
    cache_expire_time = SECONDS_PER_HOUR
    try:
        filename = pathlib.Path(gettempdir()) / "update_checker_cache.pkl"
        update_from_permacache()
    except NotImplementedError:
        filename = None

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
        now = time.time()
        key = (package_name, package_version)
        if not bypass_cache and key in cache:  # Check the in-memory cache
            cache_time, retval = cache[key]
            if now - cache_time < cache_expire_time:
                return retval
        retval = function(
            package_name=package_name,
            package_version=package_version,
            **extra_data,
        )
        cache[key] = now, retval
        if filename:
            save_to_permacache()
        return retval

    return wrapped


@cache_results
def _check(*, package_name: str, package_version: str) -> UpdateResult | None:
    data = query_pypi(
        include_prereleases=not standard_release(package_version),
        package=package_name,
    )

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


def pretty_date(the_datetime: datetime, /) -> str:  # noqa: PLR0911 -- a return per time bucket
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
    if diff.days == 1:
        return "1 day ago"
    if diff.days > 1:
        return f"{diff.days} days ago"
    if diff.seconds <= 1:
        return "just now"
    if diff.seconds < SECONDS_PER_MINUTE:
        return f"{diff.seconds} seconds ago"
    if diff.seconds < 2 * SECONDS_PER_MINUTE:
        return "1 minute ago"
    if diff.seconds < SECONDS_PER_HOUR:
        return f"{round(diff.seconds / SECONDS_PER_MINUTE)} minutes ago"
    if diff.seconds < 2 * SECONDS_PER_HOUR:
        return "1 hour ago"
    return f"{round(diff.seconds / SECONDS_PER_HOUR)} hours ago"


def query_pypi(*, include_prereleases: bool, package: str) -> dict[str, Any]:
    """Return information about the current version of package.

    Returns:
        A dict with a "success" key. On success, a "data" key maps to a dict
        with "version" and "upload_time" keys.

    """
    try:
        response = requests.get(f"https://pypi.org/pypi/{package}/json", timeout=1)
    except requests.exceptions.RequestException:
        return {"success": False}
    if response.status_code != HTTPStatus.OK:
        return {"success": False}
    data = response.json()
    versions = list(data["releases"].keys())
    versions.sort(key=parse_version, reverse=True)

    version = versions[0]
    for tmp_version in versions:
        if include_prereleases or standard_release(tmp_version):
            version = tmp_version
            break

    upload_time = None
    for file_info in data["releases"][version]:
        if file_info["upload_time"]:
            upload_time = file_info["upload_time"]
            break

    return {"success": True, "data": {"upload_time": upload_time, "version": version}}


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
        print(result, file=sys.stderr)  # noqa: T201 -- printing is the purpose
