import os
import re
from setuptools import setup

MODULE_NAME = "update_checker"

with open(os.path.join(os.path.dirname(__file__), "README.md")) as fp:
    README = fp.read()
with open(f"{MODULE_NAME}.py") as fp:
    VERSION = re.search('__version__ = "([^"]+)"', fp.read()).group(1)


extras = {
    "dev": [],
    "lint": ["black", "flake8"],
    "test": ["pytest >=2.7.3"],
}
extras["dev"] += extras["lint"] + extras["test"]

setup(
    name=MODULE_NAME,
    author="Bryce Boe",
    author_email="bbzbryce@gmail.com",
    classifiers=[
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    ],
    description="A python module that will check for package updates.",
    extras_require=extras,
    install_requires=["requests>=2.3.0"],
    license="Simplified BSD License",
    long_description=README,
    long_description_content_type="text/markdown",
    py_modules=[MODULE_NAME, f"{MODULE_NAME}_test"],
    url="https://github.com/bboe/update_checker",
    version=VERSION,
)
