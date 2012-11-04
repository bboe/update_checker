# update_checker

A python module that will check for package updates.

### Installation

The update_checker module can be installed via:

    pip install update_checker

### Usage

To see if there is a newer version of the `update_checker` package, you can use
the following bit of code:

    from update_checker import UpdateChecker
    checker = UpdateChecker()
    checker.output('update_checker', '0.0.1')