# Changelog

This project adheres to [Semantic Versioning](https://semver.org).

## 1.0.0 - 2026-06-08

First stable release, marking a commitment to the current public API.

### Added

- Asynchronous API: `async_update_check`, `UpdateChecker.async_check`, and async
  counterparts of the helpers, sharing a single core with the synchronous API.
  Install the optional extra with `uv add "update_checker[async]"`.
- The update notice written to stderr is colorized (yellow) when stderr is a
  terminal, honoring the `NO_COLOR` and `FORCE_COLOR` conventions.
- gzip-compressed responses are requested from PyPI, reducing transfer size and
  improving the odds of completing within the request timeout on slow links.
- A `py.typed` marker (PEP 561) so type checkers consume the bundled
  annotations.

### Changed

- The helpers and `UpdateChecker` methods now take keyword-only arguments;
  `update_check(package_name, package_version)` remains positional.
- Packaging migrated to uv with a `src` layout and the `uv_build` backend.
- The update notice no longer reports version-bump metadata.

### Removed

- All runtime dependencies; the package now relies solely on the standard
  library (`requests` was replaced with `urllib`).

### Fixed

- Release dates are handled as timezone-aware datetimes, fixing incorrect or
  failed date rendering on modern Python.

### Security

- The on-disk cache is JSON instead of pickle, removing an arbitrary code
  execution risk (CWE-502) when loading a tampered cache.
- PyPI responses are bounded in size and read time on both paths, and gzip
  decompression is bounded against decompression bombs.
- Package names are URL-encoded in requests.
- Permacache entries with invalid timestamps are ignored rather than crashing
  the caller, and the cache file is written with owner-only permissions.
