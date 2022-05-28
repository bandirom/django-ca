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

"""Keep track of internal settings for django-ca."""

import os
import re
import typing
import warnings
from datetime import timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding

from django.conf import global_settings
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

from .deprecation import RemovedInDjangoCA123Warning

if "CA_DIR" in os.environ:  # pragma: no cover
    CA_DIR = os.path.join(os.environ["CA_DIR"], "files")
else:
    CA_DIR = getattr(settings, "CA_DIR", os.path.join(settings.BASE_DIR, "files"))

CA_DEFAULT_KEY_SIZE: int = getattr(settings, "CA_DEFAULT_KEY_SIZE", 4096)

CA_PROFILES: typing.Dict[str, typing.Dict[str, typing.Any]] = {
    "client": {
        # see: http://security.stackexchange.com/questions/68491/
        "description": _("A certificate for a client."),
        "extensions": {
            "key_usage": {
                "critical": True,
                "value": [
                    "digitalSignature",
                ],
            },
            "extended_key_usage": {
                "critical": False,
                "value": [
                    "clientAuth",
                ],
            },
        },
    },
    "server": {
        "description": _("A certificate for a server, allows client and server authentication."),
        "extensions": {
            "key_usage": {
                "critical": True,
                "value": [
                    "digitalSignature",
                    "keyAgreement",
                    "keyEncipherment",
                ],
            },
            "extended_key_usage": {
                "critical": False,
                "value": [
                    "clientAuth",
                    "serverAuth",
                ],
            },
        },
    },
    "webserver": {
        # see http://security.stackexchange.com/questions/24106/
        "description": _("A certificate for a webserver."),
        "extensions": {
            "key_usage": {
                "critical": True,
                "value": [
                    "digitalSignature",
                    "keyAgreement",
                    "keyEncipherment",
                ],
            },
            "extended_key_usage": {
                "critical": False,
                "value": [
                    "serverAuth",
                ],
            },
        },
    },
    "enduser": {
        # see: http://security.stackexchange.com/questions/30066/
        "description": _(
            "A certificate for an enduser, allows client authentication, code and email signing."
        ),
        "cn_in_san": False,
        "extensions": {
            "key_usage": {
                "critical": True,
                "value": [
                    "dataEncipherment",
                    "digitalSignature",
                    "keyEncipherment",
                ],
            },
            "extended_key_usage": {
                "critical": False,
                "value": [
                    "clientAuth",
                    "codeSigning",
                    "emailProtection",
                ],
            },
        },
    },
    "ocsp": {
        "description": _("A certificate for an OCSP responder."),
        "cn_in_san": False,  # CAs frequently use human readable name as CN
        "add_ocsp_url": False,
        "autogenerated": True,
        "extensions": {
            "key_usage": {
                "value": [
                    "nonRepudiation",
                    "digitalSignature",
                    "keyEncipherment",
                ],
            },
            "extended_key_usage": {
                "value": [
                    "OCSPSigning",
                ],
            },
        },
    },
}

_CA_CRL_PROFILES: typing.Dict[str, typing.Dict[str, typing.Any]] = {
    "user": {
        "algorithm": "SHA512",
        "expires": 86400,
        "scope": "user",
        "encodings": [
            "PEM",
            "DER",
        ],
    },
    "ca": {
        "algorithm": "SHA512",
        "expires": 86400,
        "scope": "ca",
        "encodings": [
            "PEM",
            "DER",
        ],
    },
}

# Get and sanitize default CA serial
# NOTE: This effectively duplicates utils.sanitize_serial()
CA_DEFAULT_CA = getattr(settings, "CA_DEFAULT_CA", "").replace(":", "").upper()
if CA_DEFAULT_CA != "0":
    CA_DEFAULT_CA = CA_DEFAULT_CA.lstrip("0")
if re.search("[^0-9A-F]", CA_DEFAULT_CA):
    raise ImproperlyConfigured(f"CA_DEFAULT_CA: {CA_DEFAULT_CA}: Serial contains invalid characters.")

_SUBJECT_AS_DICT_MAPPING = "%s as a dict wil be removed in django-ca==1.23. Please use a tuple instead."
CA_DEFAULT_SUBJECT: typing.Tuple[typing.Tuple[str, str], ...] = getattr(
    settings, "CA_DEFAULT_SUBJECT", tuple()
)
if isinstance(CA_DEFAULT_SUBJECT, dict):
    # pylint: disable=consider-using-f-string # don't want to use f-string here
    warnings.warn(_SUBJECT_AS_DICT_MAPPING % "CA_DEFAULT_SUBJECT", category=RemovedInDjangoCA123Warning)
    CA_DEFAULT_SUBJECT = tuple(CA_DEFAULT_SUBJECT.items())

# Add ability just override/add some profiles
_CA_PROFILE_OVERRIDES = getattr(settings, "CA_PROFILES", {})
for name, profile in _CA_PROFILE_OVERRIDES.items():
    if profile is None:
        del CA_PROFILES[name]
        continue

    if isinstance(profile.get("subject"), dict):
        warnings.warn(
            _SUBJECT_AS_DICT_MAPPING % f"{name}: Profile subject", category=RemovedInDjangoCA123Warning
        )
        profile["subject"] = tuple(profile["subject"].items())

    if name in CA_PROFILES:
        CA_PROFILES[name].update(profile)
    else:
        CA_PROFILES[name] = profile

for name, profile in CA_PROFILES.items():
    profile.setdefault("subject", CA_DEFAULT_SUBJECT)
    profile.setdefault("cn_in_san", True)

CA_DEFAULT_ENCODING: Encoding = getattr(settings, "CA_DEFAULT_ENCODING", Encoding.PEM)
CA_DEFAULT_PROFILE = getattr(settings, "CA_DEFAULT_PROFILE", "webserver")
CA_NOTIFICATION_DAYS = getattr(
    settings,
    "CA_NOTIFICATION_DAYS",
    [
        14,
        7,
        3,
        1,
    ],
)
CA_CRL_PROFILES: typing.Dict[str, typing.Dict[str, typing.Any]] = getattr(
    settings, "CA_CRL_PROFILES", _CA_CRL_PROFILES
)
CA_PASSWORDS: typing.Dict[str, str] = getattr(settings, "CA_PASSWORDS", {})

# ACME settings
CA_ENABLE_ACME = getattr(settings, "CA_ENABLE_ACME", False)
ACME_ORDER_VALIDITY: timedelta = getattr(settings, "CA_ACME_ORDER_VALIDITY", timedelta(hours=1))
ACME_ACCOUNT_REQUIRES_CONTACT = getattr(settings, "CA_ACME_ACCOUNT_REQUIRES_CONTACT", True)
ACME_MAX_CERT_VALIDITY = getattr(settings, "CA_ACME_MAX_CERT_VALIDITY", timedelta(days=90))
ACME_DEFAULT_CERT_VALIDITY = getattr(settings, "CA_ACME_DEFAULT_CERT_VALIDITY", timedelta(days=90))

# Undocumented options, e.g. to share values between different parts of code
CA_MIN_KEY_SIZE = getattr(settings, "CA_MIN_KEY_SIZE", 2048)

CA_DEFAULT_HOSTNAME: typing.Optional[str] = getattr(settings, "CA_DEFAULT_HOSTNAME", None)

_CA_DIGEST_ALGORITHM = getattr(settings, "CA_DIGEST_ALGORITHM", "sha512").strip().upper()
try:
    CA_DIGEST_ALGORITHM: hashes.HashAlgorithm = getattr(hashes, _CA_DIGEST_ALGORITHM)()
except AttributeError:
    # pylint: disable=raise-missing-from; not really useful in this context
    raise ImproperlyConfigured(f"Unkown CA_DIGEST_ALGORITHM: {_CA_DIGEST_ALGORITHM}")

CA_DEFAULT_EXPIRES: timedelta = getattr(settings, "CA_DEFAULT_EXPIRES", timedelta(days=730))
if isinstance(CA_DEFAULT_EXPIRES, int):
    CA_DEFAULT_EXPIRES = timedelta(days=CA_DEFAULT_EXPIRES)
elif not isinstance(CA_DEFAULT_EXPIRES, timedelta):
    raise ImproperlyConfigured(f"CA_DEFAULT_EXPIRES: {CA_DEFAULT_EXPIRES}: Must be int or timedelta")
if isinstance(ACME_MAX_CERT_VALIDITY, int):
    ACME_MAX_CERT_VALIDITY = timedelta(days=ACME_MAX_CERT_VALIDITY)
if isinstance(ACME_DEFAULT_CERT_VALIDITY, int):
    ACME_DEFAULT_CERT_VALIDITY = timedelta(days=ACME_DEFAULT_CERT_VALIDITY)
if isinstance(ACME_ORDER_VALIDITY, int):
    ACME_ORDER_VALIDITY = timedelta(days=ACME_ORDER_VALIDITY)
if CA_DEFAULT_EXPIRES <= timedelta():
    raise ImproperlyConfigured(f"CA_DEFAULT_EXPIRES: {CA_DEFAULT_EXPIRES}: Must have positive value")

if CA_MIN_KEY_SIZE > CA_DEFAULT_KEY_SIZE:
    raise ImproperlyConfigured(f"CA_DEFAULT_KEY_SIZE cannot be lower then {CA_MIN_KEY_SIZE}")

_CA_DEFAULT_ECC_CURVE = getattr(settings, "CA_DEFAULT_ECC_CURVE", "SECP256R1").strip()
try:
    CA_DEFAULT_ECC_CURVE: typing.Type[ec.EllipticCurve] = getattr(ec, _CA_DEFAULT_ECC_CURVE)
    if not issubclass(CA_DEFAULT_ECC_CURVE, ec.EllipticCurve):
        raise ImproperlyConfigured(f"{_CA_DEFAULT_ECC_CURVE}: Not an EllipticCurve.")
except AttributeError:
    # pylint: disable=raise-missing-from; not really useful in this context
    raise ImproperlyConfigured(f"Unkown CA_DEFAULT_ECC_CURVE: {_CA_DEFAULT_ECC_CURVE}")

CA_FILE_STORAGE = getattr(settings, "CA_FILE_STORAGE", global_settings.DEFAULT_FILE_STORAGE)
CA_FILE_STORAGE_KWARGS = getattr(
    settings,
    "CA_FILE_STORAGE_KWARGS",
    {
        "location": CA_DIR,
        "file_permissions_mode": 0o600,
        "directory_permissions_mode": 0o700,
    },
)

CA_FILE_STORAGE_URL = "https://django-ca.readthedocs.io/en/latest/update.html#update-to-1-12-0-or-later"

# Decide if we should use Celery or not
CA_USE_CELERY = getattr(settings, "CA_USE_CELERY", None)
if CA_USE_CELERY is None:
    try:
        from celery import shared_task  # pylint: disable=unused-import

        CA_USE_CELERY = True
    except ImportError:
        CA_USE_CELERY = False
elif CA_USE_CELERY is True:
    try:
        from celery import shared_task  # NOQA: F401
    except ImportError:
        # pylint: disable=raise-missing-from; not really useful in this context
        raise ImproperlyConfigured("CA_USE_CELERY set to True, but Celery is not installed")
