#!/usr/bin/env python3
#
# This file is part of django-ca (https://github.com/mathiasertl/django-ca).
#
# django-ca is free software: you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# django-ca is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with django-ca. If not,
# see <http://www.gnu.org/licenses/>.

from termcolor import colored


def err(msg):
    print(colored("[ERR]", "red", attrs=["bold"]), msg)
    return 1


def warn(msg):
    print(colored("[WARN]", "yellow", attrs=["bold"]), msg)


def ok(msg):
    print(colored("[OK]", "green"), msg)
    return 0
