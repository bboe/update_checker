#!/usr/bin/env python
import json
import os
import pickle
import platform
import requests
import sys
import time
from datetime import datetime
from functools import wraps
from pkg_resources import parse_version as V
from tempfile import gettempdir

__version__ = '0.5'


def cache_results(function):
    def save_to_permacache():
        """Save the in-memory cache data to the permacache.

        There is a race condition here between two processes updating at the
        same time. It's perfectly acceptable to lose and/or corrupt the
        permacache information as each process's in-memory cache will remain
        in-tact.

        """
        update_from_permacache()
        with open(filename, 'wb') as fp:
            pickle.dump(cache, fp, pickle.HIGHEST_PROTOCOL)

    def update_from_permacache():
        """Attempt to update newer items from the permacache."""
        try:
            permacache = pickle.load(open(filename, 'rb'))
        except Exception:
            return
        for key, value in permacache.items():
            if key not in cache or value[0] > cache[key][0]:
                cache[key] = value

    cache = {}
    cache_expire_time = 3600
    filename = os.path.join(gettempdir(), 'update_checker_cache.pkl')
    update_from_permacache()

    @wraps(function)
    def wrapped(obj, package_name, package_version, **extra_data):
        now = time.time()
        key = (package_name, package_version)
        if key in cache:  # Check the in-memory cache
            cache_time, retval = cache[key]
            if now - cache_time < cache_expire_time:
                return retval
        retval = function(obj, package_name, package_version, **extra_data)
        cache[key] = now, retval
        save_to_permacache()
        return retval
    return wrapped


# This class must be defined before UpdateChecker in order to unpickle objects
# of this type
class UpdateResult(object):
    """Contains the information for a package that has an update."""
    def __init__(self, package, running, available, release_date):
        self.available_version = available
        self.package_name = package
        self.running_version = running
        if release_date:
            self.release_date = datetime.strptime(release_date,
                                                  '%Y-%m-%dT%H:%M:%S')
        else:
            self.release_date = None

    def __str__(self):
        retval = ('Version {0} of {1} is outdated. Version {2} '
                  .format(self.running_version, self.package_name,
                          self.available_version))
        if self.release_date:
            retval += 'was released {0}.'.format(
                pretty_date(self.release_date))
        else:
            retval += 'is available.'
        return retval


class UpdateChecker(object):
    """A class to check for package updates."""
    def __init__(self, url=None):
        """Store the URL to use for checking."""
        self.url = url if url else 'http://csil.cs.ucsb.edu:65429/check'

    @cache_results
    def check(self, package_name, package_version, **extra_data):
        """Return a UpdateResult object if there is a newer version."""
        data = extra_data
        data['package_name'] = package_name
        data['package_version'] = package_version
        data['python_version'] = sys.version.split()[0]
        data['platform'] = platform.platform(True)

        try:
            headers = {'content-type': 'application/json'}
            response = requests.put(self.url, json.dumps(data), timeout=1,
                                    headers=headers)
            data = response.json()
        except (requests.exceptions.RequestException, ValueError):
            return None

        if not data or not data.get('success') or (V(package_version) >=
                                                   V(data['data']['version'])):
            return None

        return UpdateResult(package_name, running=package_version,
                            available=data['data']['version'],
                            release_date=data['data']['upload_time'])


def pretty_date(the_datetime):
    # Source modified from
    # http://stackoverflow.com/a/5164027/176978
    diff = datetime.utcnow() - the_datetime
    if diff.days > 7 or diff.days < 0:
        return the_datetime.strftime('%A %B %d, %Y')
    elif diff.days == 1:
        return '1 day ago'
    elif diff.days > 1:
        return '{0} days ago'.format(diff.days)
    elif diff.seconds <= 1:
        return 'just now'
    elif diff.seconds < 60:
        return '{0} seconds ago'.format(diff.seconds)
    elif diff.seconds < 120:
        return '1 minute ago'
    elif diff.seconds < 3600:
        return '{0} minutes ago'.format(diff.seconds / 60)
    elif diff.seconds < 7200:
        return '1 hour ago'
    else:
        return '{0} hours ago'.format(diff.seconds / 3600)


def update_check(package_name, package_version, url=None, **extra_data):
    """Convenience method that outputs to stdout if an update is available."""
    checker = UpdateChecker(url)
    result = checker.check(package_name, package_version, **extra_data)
    if result:
        print(result)
