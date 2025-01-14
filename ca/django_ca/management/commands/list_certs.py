# This file is part of django-ca (https://github.com/mathiasertl/django-ca).
#
# django-ca is free software: you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# django-ca is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with django-ca.  If not,
# see <http://www.gnu.org/licenses/>.

"""Management command to list all available certificates.

.. seealso:: https://docs.djangoproject.com/en/dev/howto/custom-management-commands/
"""

import typing

from django.core.management.base import CommandParser
from django.utils import timezone

from ...models import Certificate
from ...models import CertificateAuthority
from ...utils import add_colons
from ..base import BaseCommand


class Command(BaseCommand):  # pylint: disable=missing-class-docstring
    help = "List all certificates."

    def add_arguments(self, parser: CommandParser) -> None:
        self.add_ca(parser, no_default=True, help_text="Only output certificates by the named authority.")
        parser.add_argument(
            "--expired", default=False, action="store_true", help="Also list expired certificates."
        )
        parser.add_argument(
            "--autogenerated",
            default=False,
            action="store_true",
            help="Also list automatically generated certificates.",
        )
        parser.add_argument(
            "--revoked", default=False, action="store_true", help="Also list revoked certificates."
        )

    def handle(  # type: ignore[override]
        self,
        ca: typing.Optional[CertificateAuthority],
        expired: bool,
        revoked: bool,
        autogenerated: bool,
        **options: typing.Any,
    ) -> None:
        certs = Certificate.objects.order_by("expires", "cn", "serial")

        if expired is False:
            certs = certs.filter(expires__gt=timezone.now())
        if revoked is False:
            certs = certs.filter(revoked=False)
        if autogenerated is False:
            certs = certs.filter(autogenerated=False)

        if ca is not None:
            certs = certs.filter(ca=ca)

        for cert in certs:
            if cert.revoked is True:
                info = "revoked"
            else:
                word = "expires"
                if cert.expires < timezone.now():
                    word = "expired"

                strftime = cert.expires.strftime("%Y-%m-%d")
                info = f"{word}: {strftime}"
            self.stdout.write(f"{add_colons(cert.serial)} - {cert.cn} ({info})")
