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
# see <http://www.gnu.org/licenses/>

"""Collection of mixin classes for unittest.TestCase subclasses."""

import copy
import io
import json
import typing
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from unittest import mock
from urllib.parse import quote

from OpenSSL.crypto import FILETYPE_PEM
from OpenSSL.crypto import X509Store
from OpenSSL.crypto import X509StoreContext
from OpenSSL.crypto import load_certificate

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed448
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import x448
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.serialization import Encoding

from django.conf import settings
from django.contrib.auth.models import User  # pylint: disable=imported-auth-user; for mypy
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError
from django.core.management import ManagementUtility
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.dispatch.dispatcher import Signal
from django.http import HttpResponse
from django.templatetags.static import static
from django.test.testcases import SimpleTestCase
from django.urls import reverse

from freezegun import freeze_time
from freezegun.api import FrozenDateTimeFactory
from freezegun.api import StepTickTimeFactory

from ... import ca_settings
from ...constants import ReasonFlags
from ...extensions import OID_TO_EXTENSION
from ...extensions import AuthorityInformationAccess
from ...extensions import AuthorityKeyIdentifier
from ...extensions import BasicConstraints
from ...extensions import CRLDistributionPoints
from ...extensions import Extension
from ...extensions import SubjectKeyIdentifier
from ...extensions.base import CRLDistributionPointsBase
from ...extensions.base import IterableExtension
from ...extensions.base import ListExtension
from ...models import Certificate
from ...models import CertificateAuthority
from ...models import DjangoCAModel
from ...models import X509CertMixin
from ...signals import post_create_ca
from ...signals import post_issue_cert
from ...signals import post_revoke_cert
from ...signals import pre_create_ca
from ...signals import pre_issue_cert
from ...subject import Subject
from ...typehints import ParsableSubject
from ...utils import ca_storage
from . import certs
from . import timestamps
from .typehints import DjangoCAModelTypeVar

if typing.TYPE_CHECKING:
    # Use SimpleTestCase as base class when type checking. This way mypy will know about attributes/methods
    # that the mixin accesses. See also:
    #   https://github.com/python/mypy/issues/5837
    TestCaseProtocol = SimpleTestCase
else:
    TestCaseProtocol = object

X509CertMixinTypeVar = typing.TypeVar("X509CertMixinTypeVar", bound=X509CertMixin)


class TestCaseMixin(TestCaseProtocol):  # pylint: disable=too-many-public-methods
    """Mixin providing augmented functionality to all test cases."""

    load_cas: typing.Union[str, typing.Tuple[str, ...]] = tuple()
    load_certs: typing.Union[str, typing.Tuple[str, ...]] = tuple()
    default_ca = "child"
    default_cert = "child-cert"
    cas: typing.Dict[str, CertificateAuthority] = {}
    certs: typing.Dict[str, Certificate] = {}

    # Note: cryptography sometimes adds another sentence at the end
    re_false_password = r"^Could not decrypt private key - bad password\?$"

    def setUp(self) -> None:  # pylint: disable=invalid-name,missing-function-docstring
        super().setUp()
        cache.clear()

        self.cas = {}
        self.certs = {}
        self.load_cas = self.load_named_cas(self.load_cas)
        self.load_certs = self.load_named_certs(self.load_certs)

        # Set `self.ca` as a default certificate authority (if at least one is loaded)
        if len(self.load_cas) == 1:  # only one CA specified, set self.ca for convenience
            self.ca = self.cas[self.load_cas[0]]
        elif self.load_cas:
            if self.default_ca not in self.load_cas:  # pragma: no cover
                self.fail(f"{self.default_ca}: Not in {self.load_cas}.")
            self.ca = self.cas[self.default_ca]

        # Set `self.cert` as a default certificate (if at least one is loaded)
        if len(self.load_certs) == 1:  # only one CA specified, set self.cert for convenience
            self.cert = self.certs[self.load_certs[0]]
        elif self.load_certs:
            if self.default_cert not in self.load_certs:  # pragma: no cover
                self.fail(f"{self.default_cert}: Not in {self.load_certs}.")
            self.cert = self.certs[self.default_cert]

    def load_named_cas(self, cas: typing.Union[str, typing.Tuple[str, ...]]) -> typing.Tuple[str, ...]:
        """Load CAs by the given name."""
        if cas == "__all__":
            cas = tuple(k for k, v in certs.items() if v.get("type") == "ca")
        elif cas == "__usable__":
            cas = tuple(k for k, v in certs.items() if v.get("type") == "ca" and v["key_filename"])
        elif isinstance(cas, str):  # pragma: no cover
            self.fail(f"{cas}: Unknown alias for load_cas.")

        # Filter CAs that we already loaded
        cas = tuple(ca for ca in cas if ca not in self.cas)

        # Load all CAs (sort by len() of parent so that root CAs are loaded first)
        for name in sorted(cas, key=lambda n: len(certs[n].get("parent", ""))):
            self.cas[name] = self.load_ca(name)
        return cas

    def load_named_certs(self, names: typing.Union[str, typing.Tuple[str, ...]]) -> typing.Tuple[str, ...]:
        """Load certs by the given name."""
        if names == "__all__":
            names = tuple(k for k, v in certs.items() if v.get("type") == "cert")
        elif names == "__usable__":
            names = tuple(k for k, v in certs.items() if v.get("type") == "cert" and v["cat"] == "generated")
        elif isinstance(names, str):  # pragma: no cover
            self.fail(f"{names}: Unknown alias for load_certs.")

        # Filter certificates that are already loaded
        names = tuple(name for name in names if name not in self.certs)

        for name in names:
            try:
                self.certs[name] = self.load_named_cert(name)
            except CertificateAuthority.DoesNotExist:  # pragma: no cover
                self.fail(f'{certs[name]["ca"]}: Could not load CertificateAuthority.')
        return names

    def absolute_uri(self, name: str, hostname: typing.Optional[str] = None, **kwargs: typing.Any) -> str:
        """Build an absolute uri for the given request.

        The `name` is assumed to be a URL name or a full path. If `name` starts with a colon, ``django_ca``
        is used as namespace.
        """

        if hostname is None:
            hostname = settings.ALLOWED_HOSTS[0]

        if name.startswith("/"):
            return f"http://{hostname}{name}"
        if name.startswith(":"):  # pragma: no branch
            name = f"django_ca{name}"
        return f"http://{hostname}{reverse(name, kwargs=kwargs)}"

    def assertAuthorityKeyIdentifier(  # pylint: disable=invalid-name
        self, issuer: CertificateAuthority, cert: X509CertMixin
    ) -> None:
        """Test the key identifier of the AuthorityKeyIdentifier extenion of `cert`."""
        self.assertEqual(
            cert.authority_key_identifier.key_identifier,  # type: ignore[union-attr] # aki theoretically None
            issuer.subject_key_identifier.value,  # type: ignore[union-attr] # ski theoretically None
        )

    def assertBasic(  # pylint: disable=invalid-name
        self, cert: x509.Certificate, algo: typing.Type[hashes.HashAlgorithm] = hashes.SHA256
    ) -> None:
        """Assert some basic key properties."""
        self.assertEqual(cert.version, x509.Version.v3)
        self.assertIsInstance(cert.public_key(), rsa.RSAPublicKey)
        self.assertIsInstance(cert.signature_hash_algorithm, algo)

    def assertCRL(
        # pylint: disable=invalid-name
        self,
        crl: bytes,
        expected: typing.Optional[typing.Sequence[X509CertMixin]] = None,
        signer: typing.Optional[CertificateAuthority] = None,
        expires: int = 86400,
        algorithm: typing.Optional[hashes.HashAlgorithm] = None,
        encoding: Encoding = Encoding.PEM,
        idp: typing.Optional["x509.Extension[x509.IssuingDistributionPoint]"] = None,
        extensions: typing.Optional[typing.List["x509.Extension[x509.ExtensionType]"]] = None,
        crl_number: int = 0,
    ) -> None:
        """Test the given CRL.

        Parameters
        ----------

        crl : bytes
            The raw CRL
        expected : list
            List of CAs/certs to be expected in this CRL
        """
        expected = expected or []
        signer = signer or self.cas["child"]
        algorithm = algorithm or ca_settings.CA_DIGEST_ALGORITHM
        extensions = extensions or []
        expires_timestamp = datetime.utcnow() + timedelta(seconds=expires)

        if idp is not None:  # pragma: no branch
            extensions.append(idp)  # type: ignore[arg-type] # why is this not recognized?
        extensions.append(
            x509.Extension(
                value=x509.CRLNumber(crl_number=crl_number),
                critical=False,
                oid=x509.oid.ExtensionOID.CRL_NUMBER,
            )
        )
        extensions.append(
            x509.Extension(
                value=signer.get_authority_key_identifier(),
                oid=x509.oid.ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
                critical=False,
            )
        )

        if encoding == Encoding.PEM:
            parsed_crl = x509.load_pem_x509_crl(crl, default_backend())
        else:
            parsed_crl = x509.load_der_x509_crl(crl, default_backend())

        public_key = signer.pub.loaded.public_key()
        if isinstance(public_key, (x448.X448PublicKey, x25519.X25519PublicKey)):  # pragma: no cover
            raise TypeError()  # just to make mypy happy

        self.assertIsInstance(parsed_crl.signature_hash_algorithm, type(algorithm))
        self.assertTrue(parsed_crl.is_signature_valid(public_key))
        self.assertEqual(parsed_crl.issuer, signer.pub.loaded.subject)
        self.assertEqual(parsed_crl.last_update, datetime.utcnow())
        self.assertEqual(parsed_crl.next_update, expires_timestamp)
        self.assertCountEqual(list(parsed_crl.extensions), extensions)

        entries = {e.serial_number: e for e in parsed_crl}
        self.assertCountEqual(entries, {c.pub.loaded.serial_number: c for c in expected})
        for entry in entries.values():
            self.assertEqual(entry.revocation_date, datetime.utcnow())
            self.assertEqual(list(entry.extensions), [])

    @contextmanager
    def assertCommandError(self, msg: str) -> typing.Iterator[None]:  # pylint: disable=invalid-name
        """Context manager asserting that CommandError is raised.

        Parameters
        ----------

        msg : str
            The regex matching the exception message.
        """
        with self.assertRaisesRegex(CommandError, msg):
            yield

    @contextmanager
    def assertCreateCASignals(  # pylint: disable=invalid-name
        self, pre: bool = True, post: bool = True
    ) -> typing.Iterator[typing.Tuple[mock.Mock, mock.Mock]]:
        """Context manager mocking both pre and post_create_ca signals."""
        with self.mockSignal(pre_create_ca) as pre_sig, self.mockSignal(post_create_ca) as post_sig:
            try:
                yield (pre_sig, post_sig)
            finally:
                self.assertTrue(pre_sig.called is pre)
                self.assertTrue(post_sig.called is post)

    @contextmanager
    def assertCreateCertSignals(  # pylint: disable=invalid-name
        self, pre: bool = True, post: bool = True
    ) -> typing.Iterator[typing.Tuple[mock.Mock, mock.Mock]]:
        """Context manager mocking both pre and post_create_ca signals."""
        with self.mockSignal(pre_issue_cert) as pre_sig, self.mockSignal(post_issue_cert) as post_sig:
            try:
                yield (pre_sig, post_sig)
            finally:
                self.assertTrue(pre_sig.called is pre)
                self.assertTrue(post_sig.called is post)

    def assertExtensions(  # pylint: disable=invalid-name
        self,
        cert: typing.Union[X509CertMixin, x509.Certificate],
        extensions: typing.Iterable[Extension[typing.Any, typing.Any, typing.Any]],
        signer: typing.Optional[CertificateAuthority] = None,
        expect_defaults: bool = True,
    ) -> None:
        """Assert that `cert` has the given extensions."""
        mapped_extensions = {e.key: e for e in extensions}

        if isinstance(cert, Certificate):
            pubkey = cert.pub.loaded.public_key()
            # TYPE NOTE: only used for CAs with known extensions, so this is never a x509.Extension
            actual = {e.key: e for e in cert.extensions}  # type: ignore[union-attr]
            signer = cert.ca
        elif isinstance(cert, CertificateAuthority):
            pubkey = cert.pub.loaded.public_key()
            # TYPE NOTE: only used for CAs with known extensions, so this is never a x509.Extension
            actual = {e.key: e for e in cert.extensions}  # type: ignore[union-attr]

            if cert.parent is None:  # root CA
                signer = cert
            else:  # intermediate CA
                signer = cert.parent
        elif isinstance(cert, x509.Certificate):  # cg cert
            pubkey = cert.public_key()
            actual = {
                e.key: e
                for e in [
                    OID_TO_EXTENSION[e.oid](e) if e.oid in OID_TO_EXTENSION else e for e in cert.extensions
                ]
            }
        else:  # pragma: no cover
            raise ValueError("cert must be Certificate(Authority) or x509.Certificate)")

        if expect_defaults is True:
            if isinstance(cert, Certificate):
                mapped_extensions.setdefault(BasicConstraints.key, BasicConstraints())
            if signer is not None:
                mapped_extensions.setdefault(
                    AuthorityKeyIdentifier.key, signer.get_authority_key_identifier_extension()
                )

                if isinstance(cert, Certificate) and signer.crl_url:
                    urls = signer.crl_url.split()
                    ext = CRLDistributionPoints({"value": [{"full_name": urls}]})
                    mapped_extensions.setdefault(CRLDistributionPoints.key, ext)

                aia = AuthorityInformationAccess()
                if isinstance(cert, Certificate) and signer.ocsp_url:
                    aia.ocsp = [signer.ocsp_url]
                if isinstance(cert, Certificate) and signer.issuer_url:
                    aia.issuers = [signer.issuer_url]
                if aia.ocsp or aia.issuers:
                    mapped_extensions.setdefault(AuthorityInformationAccess.key, aia)

            ski = x509.SubjectKeyIdentifier.from_public_key(pubkey)
            mapped_extensions.setdefault(SubjectKeyIdentifier.key, SubjectKeyIdentifier(ski))

        self.assertEqual(actual, mapped_extensions)

    @contextmanager
    def assertImproperlyConfigured(self, msg: str) -> typing.Iterator[None]:  # pylint: disable=invalid-name
        """Shortcut for testing that the code raises ImproperlyConfigured with the given message."""
        with self.assertRaisesRegex(ImproperlyConfigured, msg):
            yield

    def assertIssuer(  # pylint: disable=invalid-name
        self, issuer: CertificateAuthority, cert: X509CertMixin
    ) -> None:
        """Assert that the issuer for `cert` matches the subject of `issuer`."""
        self.assertEqual(cert.issuer, issuer.subject)

    def assertMessages(  # pylint: disable=invalid-name
        self, response: HttpResponse, expected: typing.List[str]
    ) -> None:
        """Assert given Django messages for `response`."""
        messages = [str(m) for m in list(get_messages(response.wsgi_request))]
        self.assertEqual(messages, expected)

    def assertNotRevoked(self, cert: X509CertMixin) -> None:  # pylint: disable=invalid-name
        """Assert that the certificate is not revoked."""
        cert.refresh_from_db()
        self.assertFalse(cert.revoked)
        self.assertEqual(cert.revoked_reason, "")

    def assertPostCreateCa(  # pylint: disable=invalid-name
        self, post: mock.Mock, ca: CertificateAuthority
    ) -> None:
        """Assert that the post_create_ca signal was called."""
        post.assert_called_once_with(ca=ca, signal=post_create_ca, sender=CertificateAuthority)

    def assertPostIssueCert(self, post: mock.Mock, cert: Certificate) -> None:  # pylint: disable=invalid-name
        """Assert that the post_issue_cert signal was called."""
        post.assert_called_once_with(cert=cert, signal=post_issue_cert, sender=Certificate)

    def assertPostRevoke(self, post: mock.Mock, cert: Certificate) -> None:  # pylint: disable=invalid-name
        """Assert that the post_revoke_cert signal was called."""
        post.assert_called_once_with(cert=cert, signal=post_revoke_cert, sender=Certificate)

    def assertPrivateKey(  # pylint: disable=invalid-name
        self, ca: CertificateAuthority, password: typing.Optional[typing.Union[str, bytes]] = None
    ) -> None:
        """Assert some basic properties for a private key."""
        key = ca.key(password)
        self.assertIsNotNone(key)
        if not isinstance(  # pragma: no branch  # only used for RSA keys
            key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)
        ):
            self.assertTrue(key.key_size > 0)

    def assertRevoked(  # pylint: disable=invalid-name
        self, cert: X509CertMixin, reason: typing.Optional[str] = None
    ) -> None:
        """Assert that the certificate is now revoked."""
        if isinstance(cert, CertificateAuthority):
            cert = CertificateAuthority.objects.get(serial=cert.serial)
        else:
            cert = Certificate.objects.get(serial=cert.serial)

        self.assertTrue(cert.revoked)

        if reason is None:
            self.assertEqual(cert.revoked_reason, ReasonFlags.unspecified.name)
        else:
            self.assertEqual(cert.revoked_reason, reason)

    def assertSignature(  # pylint: disable=invalid-name
        self,
        chain: typing.Iterable[CertificateAuthority],
        cert: typing.Union[Certificate, CertificateAuthority],
    ) -> None:
        """Assert that `cert` is properly signed by `chain`.

        .. seealso:: http://stackoverflow.com/questions/30700348
        """
        store = X509Store()

        # set the time of the OpenSSL context - freezegun doesn't work, because timestamp comes from OpenSSL
        now = datetime.utcnow()
        store.set_time(now)

        for elem in chain:
            ca = load_certificate(FILETYPE_PEM, elem.pub.pem.encode())
            store.add_cert(ca)

            # Verify that the CA itself is valid
            store_ctx = X509StoreContext(store, ca)
            self.assertIsNone(store_ctx.verify_certificate())  # type: ignore[func-returns-value]

        loaded_cert = load_certificate(FILETYPE_PEM, cert.pub.pem.encode())
        store_ctx = X509StoreContext(store, loaded_cert)
        self.assertIsNone(store_ctx.verify_certificate())  # type: ignore[func-returns-value]

    def assertSubject(  # pylint: disable=invalid-name
        self, cert: x509.Certificate, expected: typing.Union[Subject, ParsableSubject]
    ) -> None:
        """Assert the subject of `cert` matches `expected`."""
        if not isinstance(expected, Subject):
            expected = Subject(expected)
        self.assertEqual(Subject([(s.oid, s.value) for s in cert.subject]), expected)

    @contextmanager
    def assertSystemExit(self, code: int) -> typing.Iterator[None]:  # pylint: disable=invalid-name
        """Assert that SystemExit is raised."""
        with self.assertRaisesRegex(SystemExit, fr"^{code}$") as excm:
            yield
        self.assertEqual(excm.exception.args, (code,))

    @contextmanager
    def assertValidationError(  # pylint: disable=invalid-name; unittest standard
        self, errors: typing.Dict[str, typing.List[str]]
    ) -> typing.Iterator[None]:
        """Context manager to assert that a ValidationError is thrown."""
        with self.assertRaises(ValidationError) as cmex:
            yield
        self.assertEqual(cmex.exception.message_dict, errors)

    @property
    def ca_certs(self) -> typing.Iterator[typing.Tuple[str, Certificate]]:
        """Yield loaded certificates for each certificate authority."""
        for name, cert in self.certs.items():
            if name in ["root-cert", "child-cert", "ecc-cert", "dsa-cert", "pwd-cert"]:
                yield name, cert

    @typing.overload
    def cmd(
        self, *args: typing.Any, stdout: io.BytesIO, stderr: io.BytesIO, **kwargs: typing.Any
    ) -> typing.Tuple[bytes, bytes]:
        ...

    @typing.overload
    def cmd(
        self,
        *args: typing.Any,
        stdout: io.BytesIO,
        stderr: typing.Optional[io.StringIO] = None,
        **kwargs: typing.Any,
    ) -> typing.Tuple[bytes, str]:
        ...

    @typing.overload
    def cmd(
        self,
        *args: typing.Any,
        stdout: typing.Optional[io.StringIO] = None,
        stderr: io.BytesIO,
        **kwargs: typing.Any,
    ) -> typing.Tuple[str, bytes]:
        ...

    @typing.overload
    def cmd(
        self,
        *args: typing.Any,
        stdout: typing.Optional[io.StringIO] = None,
        stderr: typing.Optional[io.StringIO] = None,
        **kwargs: typing.Any,
    ) -> typing.Tuple[str, str]:
        ...

    def cmd(
        self,
        *args: typing.Any,
        stdout: typing.Optional[typing.Union[io.StringIO, io.BytesIO]] = None,
        stderr: typing.Optional[typing.Union[io.StringIO, io.BytesIO]] = None,
        **kwargs: typing.Any,
    ) -> typing.Tuple[typing.Union[str, bytes], typing.Union[str, bytes]]:
        """Call to a manage.py command using call_command."""
        if stdout is None:
            stdout = io.StringIO()
        if stderr is None:
            stderr = io.StringIO()
        stdin = kwargs.pop("stdin", io.StringIO())

        if isinstance(stdin, io.StringIO):
            with mock.patch("sys.stdin", stdin):
                call_command(*args, stdout=stdout, stderr=stderr, **kwargs)
        else:
            # mock https://docs.python.org/3/library/io.html#io.BufferedReader.read
            def _read_mock(size=None):  # type: ignore # pylint: disable=unused-argument
                return stdin

            with mock.patch("sys.stdin.buffer.read", side_effect=_read_mock):
                call_command(*args, stdout=stdout, stderr=stderr, **kwargs)

        return stdout.getvalue(), stderr.getvalue()

    def cmd_e2e(
        self,
        cmd: typing.Sequence[str],
        stdin: typing.Optional[typing.Union[io.StringIO, bytes]] = None,
        stdout: typing.Optional[io.StringIO] = None,
        stderr: typing.Optional[io.StringIO] = None,
    ) -> typing.Tuple[str, str]:
        """Call a management command the way manage.py does.

        Unlike call_command, this method also tests the argparse configuration of the called command.
        """
        stdout = stdout or io.StringIO()
        stderr = stderr or io.StringIO()
        if stdin is None:
            stdin = io.StringIO()

        if isinstance(stdin, io.StringIO):
            stdin_mock = mock.patch("sys.stdin", stdin)
        else:

            def _read_mock(size=None):  # type: ignore # pylint: disable=unused-argument
                return stdin

            # TYPE NOTE: mypy detects a different type, but important thing is its a context manager
            stdin_mock = mock.patch(  # type: ignore[assignment]
                "sys.stdin.buffer.read", side_effect=_read_mock
            )

        with stdin_mock, mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            util = ManagementUtility(["manage.py"] + list(cmd))
            util.execute()

        return stdout.getvalue(), stderr.getvalue()

    def cmd_help_text(self, cmd: str) -> str:
        """Get the help message for a given management command.

        Also asserts that stderr is empty and the command exists with status code 0."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            util = ManagementUtility(["manage.py", cmd, "--help"])
            with self.assertSystemExit(0):
                util.execute()

        self.assertEqual(stderr.getvalue(), "")
        return stdout.getvalue()

    @classmethod
    def create_cert(
        cls,
        ca: CertificateAuthority,
        csr: x509.CertificateSigningRequest,
        subject: typing.Optional[Subject],
        **kwargs: typing.Any,
    ) -> Certificate:
        """Create a certificate with the given data."""
        cert = Certificate.objects.create_cert(ca, csr, subject=subject, **kwargs)
        cert.full_clean()
        return cert

    @property
    def crl_profiles(self) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
        """Return a list of CRL profiles."""
        profiles = copy.deepcopy(ca_settings.CA_CRL_PROFILES)
        for config in profiles.values():
            config.setdefault("OVERRIDES", {})

            for data in [d for d in certs.values() if d.get("type") == "ca"]:
                config["OVERRIDES"][data["serial"]] = {}
                if data.get("password"):
                    config["OVERRIDES"][data["serial"]]["password"] = data["password"]

        return profiles

    def get_idp(
        self,
        full_name: typing.Optional[typing.Iterable[x509.GeneralName]] = None,
        indirect_crl: bool = False,
        only_contains_attribute_certs: bool = False,
        only_contains_ca_certs: bool = False,
        only_contains_user_certs: bool = False,
        only_some_reasons: typing.Optional[typing.FrozenSet[x509.ReasonFlags]] = None,
        relative_name: typing.Optional[x509.RelativeDistinguishedName] = None,
    ) -> "x509.Extension[x509.IssuingDistributionPoint]":
        """Get an IssuingDistributionPoint extension."""
        return x509.Extension(
            oid=x509.oid.ExtensionOID.ISSUING_DISTRIBUTION_POINT,
            value=x509.IssuingDistributionPoint(
                full_name=full_name,
                indirect_crl=indirect_crl,
                only_contains_attribute_certs=only_contains_attribute_certs,
                only_contains_ca_certs=only_contains_ca_certs,
                only_contains_user_certs=only_contains_user_certs,
                only_some_reasons=only_some_reasons,
                relative_name=relative_name,
            ),
            critical=True,
        )

    def get_idp_full_name(
        self, ca: CertificateAuthority
    ) -> typing.Optional[typing.List[x509.UniformResourceIdentifier]]:
        """Get the IDP full name for `ca`."""
        crl_url = [url.strip() for url in ca.crl_url.split()]
        return [x509.UniformResourceIdentifier(c) for c in crl_url] or None

    @classmethod
    def expires(cls, days: int) -> datetime:
        """Get a timestamp `days` from now."""
        now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return now + timedelta(days + 1)

    @contextmanager
    def freeze_time(
        self, timestamp: typing.Union[str, datetime]
    ) -> typing.Iterator[typing.Union[FrozenDateTimeFactory, StepTickTimeFactory]]:
        """Context manager to freeze time to a given timestamp.

        If `timestamp` is a str that is in the `timestamps` dict (e.g. "everything-valid"), use that
        timestamp.
        """
        if isinstance(timestamp, str):  # pragma: no branch
            timestamp = timestamps[timestamp]

        with freeze_time(timestamp) as frozen:
            yield frozen

    def get_cert_context(self, name: str) -> typing.Dict[str, typing.Any]:
        """Get a dictionary suitable for testing output based on the dictionary in basic.certs."""
        ctx: typing.Dict[str, typing.Any] = {}
        for key, value in certs[name].items():
            if key == "precert_poison":
                ctx["precert_poison"] = "PrecertPoison (critical): Yes"
            elif key == "precertificate_signed_certificate_timestamps_serialized":
                ctx["sct_critical"] = " (critical)" if value["critical"] else ""
                ctx["sct_values"] = []
                for val in value["value"]:
                    ctx["sct_values"].append(val)
            elif key == "precertificate_signed_certificate_timestamps":
                continue  # special extension b/c it cannot be created
            elif key == "pathlen":
                ctx[key] = value
                ctx[f"{key}_text"] = "unlimited" if value is None else value
            elif isinstance(value, Extension):
                ctx[key] = value

                if isinstance(value, ListExtension):
                    for i, val in enumerate(value):
                        ctx[f"{key}_{i}"] = val

                else:
                    ctx[f"{key}_text"] = value.as_text()

                if value.critical:
                    ctx[f"{key}_critical"] = " (critical)"
                else:
                    ctx[f"{key}_critical"] = ""
            else:
                ctx[key] = value

            if isinstance(value, CRLDistributionPointsBase):
                for i, ext_value in enumerate(value.value):
                    ctx[f"{key}_{i}"] = ext_value
            elif isinstance(value, IterableExtension):
                for i, ext_value in enumerate(value.serialize_value()):
                    ctx[f"{key}_{i}"] = ext_value

        if certs[name].get("parent"):
            parent = certs[certs[name]["parent"]]
            ctx["parent_name"] = parent["name"]
            ctx["parent_serial"] = parent["serial"]

        if certs[name]["key_filename"] is not False:
            ctx["key_path"] = ca_storage.path(certs[name]["key_filename"])
        return ctx

    @classmethod
    def load_ca(
        cls,
        name: str,
        parsed: typing.Optional[x509.Certificate] = None,
        enabled: bool = True,
        parent: typing.Optional[CertificateAuthority] = None,
        **kwargs: typing.Any,
    ) -> CertificateAuthority:
        """Load a CA from one of the preloaded files."""
        path = f"{name}.key"
        if parsed is None:
            parsed = certs[name]["pub"]["parsed"]
        if parent is None and certs[name].get("parent"):
            parent = CertificateAuthority.objects.get(name=certs[name]["parent"])

        # set some default values
        kwargs.setdefault("issuer_alt_name", certs[name].get("issuer_alternative_name", ""))
        kwargs.setdefault("crl_url", certs[name].get("crl_url", ""))
        kwargs.setdefault("ocsp_url", certs[name].get("ocsp_url", ""))
        kwargs.setdefault("issuer_url", certs[name].get("issuer_url", ""))

        ca = CertificateAuthority(name=name, private_key_path=path, enabled=enabled, parent=parent, **kwargs)
        ca.update_certificate(parsed)  # calculates serial etc
        ca.save()
        return ca

    @classmethod
    def load_named_cert(cls, name: str) -> Certificate:
        """Load a certificate with the given mame."""
        data = certs[name]
        ca = CertificateAuthority.objects.get(name=data["ca"])
        csr = data.get("csr", {}).get("parsed", "")
        profile = data.get("profile", "")

        cert = Certificate(ca=ca, csr=csr, profile=profile)
        cert.update_certificate(data["pub"]["parsed"])
        cert.save()
        cert.refresh_from_db()  # make sure we have lazy fields set
        return cert

    @contextmanager
    def mockSignal(self, signal: Signal) -> typing.Iterator[mock.Mock]:  # pylint: disable=invalid-name
        """Context manager to attach a mock to the given signal."""

        # This function is only here to create an autospec. From the documentation:
        #
        #   Notice that the function takes a sender argument, along with wildcard keyword arguments
        #   (**kwargs); all signal handlers must take these arguments.
        #
        # https://docs.djangoproject.com/en/dev/topics/signals/#connecting-to-specific-signals
        def callback(sender: models.Model, **kwargs: typing.Any) -> None:  # pragma: no cover
            # pylint: disable=unused-argument
            pass

        signal_mock = mock.create_autospec(callback, spec_set=True)
        signal.connect(signal_mock)
        try:
            yield signal_mock
        finally:
            signal.disconnect(signal_mock)

    @contextmanager
    def mute_celery(self, *calls: typing.Any) -> typing.Iterator[mock.MagicMock]:
        """Context manager to mock celery invocations.

        This context manager mocks ``celery.app.task.Task.apply_async``, the final function in celery before
        the message is passed to the handlers for the configured message transport (Redis, MQTT, ...). The
        context manager will validate the mock was called as specified in the passed *calls* arguments.

        The context manager will also assert that the args and kwargs passed to the tasks are JSON
        serializable.

        .. WARNING::

           The args and kwargs passed to the task are the first and second *argument* passed to the mocked
           ``apply_async``. You must consider this when passing calls. For example::

               with self.mute_celery((((), {}), {})):
                   cache_crls.delay()

               with self.mute_celery(((("foo"), {"key": "bar"}), {})):
                   cache_crls.delay("foo", key="bar")
        """

        with mock.patch("celery.app.task.Task.apply_async", spec_set=True) as mocked:
            yield mocked

        # Make sure that all invocations are JSON serializable
        for invocation in mocked.call_args_list:
            # invocation apply_async() has task args as arg[0] and arg[1]
            self.assertIsInstance(json.dumps(invocation.args[0]), str)
            self.assertIsInstance(json.dumps(invocation.args[1]), str)

        # Make sure that task was called the right number of times
        self.assertEqual(len(calls), len(mocked.call_args_list))
        for expected, actual in zip(calls, mocked.call_args_list):
            self.assertEqual(expected, actual)

    @contextmanager
    def patch(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Iterator[mock.MagicMock]:
        """Shortcut to :py:func:`py:unittest.mock.patch`."""
        with mock.patch(*args, **kwargs) as mocked:
            yield mocked

    @contextmanager
    def patch_object(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Iterator[typing.Any]:
        """Shortcut to :py:func:`py:unittest.mock.patch.object`."""
        with mock.patch.object(*args, **kwargs) as mocked:
            yield mocked

    def reverse(self, name: str, *args: typing.Any, **kwargs: typing.Any) -> str:
        """Shortcut to reverse an URI name."""
        return reverse(f"django_ca:{name}", args=args, kwargs=kwargs)

    def uri(self, uri: str) -> x509.UniformResourceIdentifier:
        """Minor shortcast to get a x509.UniformResourceIdentifier."""
        return x509.UniformResourceIdentifier(uri)

    @property
    def usable_cas(self) -> typing.Iterator[typing.Tuple[str, CertificateAuthority]]:
        """Yield loaded generated certificates."""
        for name, ca in self.cas.items():
            if certs[name]["key_filename"]:
                yield name, ca

    @property
    def usable_certs(self) -> typing.Iterator[typing.Tuple[str, Certificate]]:
        """Yield loaded generated certificates."""
        for name, cert in self.certs.items():
            if certs[name]["cat"] == "generated":
                yield name, cert


class AdminTestCaseMixin(TestCaseMixin, typing.Generic[DjangoCAModelTypeVar]):
    """Common mixin for testing admin classes for models."""

    model: typing.Type[DjangoCAModelTypeVar]
    """Model must be configured for TestCase instances using this mixin."""

    media_css: typing.Tuple[str, ...] = tuple()
    """List of custom CSS files loaded by the ModelAdmin.Media class."""

    view_name: str
    """The name of the view being tested."""

    # TODO: we should get rid of this, it's ugly
    obj: typing.Optional[DjangoCAModel]

    def setUp(self) -> None:  # pylint: disable=invalid-name,missing-function-docstring
        super().setUp()
        self.user = self.create_superuser()
        self.client.force_login(self.user)
        self.obj = self.model.objects.first()  # TODO: get rid of this

    @property
    def add_url(self) -> str:
        """Shortcut for the "add" URL of the model under test."""
        return typing.cast(str, self.model.admin_add_url)  # type hinting for @classproperty doesn't work

    def assertBundle(  # pylint: disable=invalid-name
        self, cert: DjangoCAModelTypeVar, expected: typing.Iterable[X509CertMixin], filename: str
    ) -> None:
        """Assert that the bundle for the given certificate matches the expected chain and filename."""
        url = self.get_url(cert)
        expected_content = "\n".join([e.pub.pem.strip() for e in expected]) + "\n"
        response = self.client.get(url, {"format": "PEM"})
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response["Content-Type"], "application/pkix-cert")
        self.assertEqual(response["Content-Disposition"], f"attachment; filename={filename}")
        self.assertEqual(response.content.decode("utf-8"), expected_content)

    def assertCSS(self, response: HttpResponse, path: str) -> None:  # pylint: disable=invalid-name
        """Assert that the HTML from the given response includes the mentioned CSS."""
        css = f'<link href="{static(path)}" type="text/css" media="all" rel="stylesheet" />'
        self.assertInHTML(css, response.content.decode("utf-8"), 1)

    def assertChangeResponse(  # pylint: disable=invalid-name,unused-argument # obj is unused
        self, response: HttpResponse, obj: DjangoCAModelTypeVar, status: int = HTTPStatus.OK
    ) -> None:
        """Assert that the passed response is a model change view."""
        self.assertEqual(response.status_code, status)
        templates = [t.name for t in response.templates]
        self.assertIn("admin/change_form.html", templates)
        self.assertIn("admin/base.html", templates)

        for css in self.media_css:
            self.assertCSS(response, css)

    def assertChangelistResponse(  # pylint: disable=invalid-name
        self, response: HttpResponse, *objects: models.Model, status: int = HTTPStatus.OK
    ) -> None:
        """Assert that the passed response is a model changelist view."""
        self.assertEqual(response.status_code, status)
        self.assertCountEqual(response.context["cl"].result_list, objects)

        templates = [t.name for t in response.templates]
        self.assertIn("admin/base.html", templates)
        self.assertIn("admin/change_list.html", templates)

        for css in self.media_css:
            self.assertCSS(response, css)

    def assertRequiresLogin(  # pylint: disable=invalid-name
        self, response: HttpResponse, **kwargs: typing.Any
    ) -> None:
        """Assert that the given response is a redirect to the login page."""
        path = reverse("admin:login")
        qs = quote(response.wsgi_request.get_full_path())
        self.assertRedirects(response, f"{path}?next={qs}", **kwargs)

    def change_url(self, obj: typing.Optional[DjangoCAModel] = None) -> str:
        """Shortcut for the change URL of the given instance."""
        obj = obj or self.obj
        return obj.admin_change_url  # type: ignore[union-attr]

    @property
    def changelist_url(self) -> str:
        """Shortcut for the changelist URL of the model under test."""
        return typing.cast(str, self.model.admin_changelist_url)

    def create_superuser(
        self, username: str = "admin", password: str = "admin", email: str = "user@example.com"
    ) -> User:
        """Shortcut to create a superuser."""
        return User.objects.create_superuser(username=username, password=password, email=email)

    @contextmanager
    def freeze_time(
        self, timestamp: typing.Union[str, datetime]
    ) -> typing.Iterator[typing.Union[FrozenDateTimeFactory, StepTickTimeFactory]]:
        """Overridden to force a client login, otherwise the user session is expired."""

        with super().freeze_time(timestamp) as frozen:
            self.client.force_login(self.user)
            yield frozen

    def get_changelist_view(self, data: typing.Optional[typing.Dict[str, str]] = None) -> HttpResponse:
        """Get the response to a changelist view for the given model."""
        return self.client.get(self.changelist_url, data)

    def get_change_view(
        self, obj: DjangoCAModelTypeVar, data: typing.Optional[typing.Dict[str, str]] = None
    ) -> HttpResponse:
        """Get the response to a change view for the given model instance."""
        return self.client.get(self.change_url(obj), data)

    def get_objects(self) -> typing.Iterable[DjangoCAModelTypeVar]:
        """Get list of objects for defined for this test."""
        return self.model.objects.all()

    def get_url(self, obj: DjangoCAModelTypeVar) -> str:
        """Get URL for the given object for this test case."""
        return reverse(f"admin:{self.view_name}", kwargs={"pk": obj.pk})


class StandardAdminViewTestCaseMixin(AdminTestCaseMixin[DjangoCAModelTypeVar]):
    """A mixin that adds tests for the standard Django admin views.

    TestCases using this mixin are expected to implement ``setUp`` to add some useful test model instances.
    """

    def get_changelists(
        self,
    ) -> typing.Iterator[typing.Tuple[typing.Iterable[DjangoCAModel], typing.Dict[str, str]]]:
        """Generator for possible changelist views.

        Should yield tuples of objects that should be displayed and a dict of query parameters.
        """
        yield (self.model.objects.all(), {})

    def test_model_count(self) -> None:
        """Test that the implementing TestCase actually creates some instances."""
        self.assertGreater(self.model.objects.all().count(), 0)

    def test_changelist_view(self) -> None:
        """Test that the changelist view works."""
        for qs, data in self.get_changelists():
            self.assertChangelistResponse(self.get_changelist_view(data), *qs)

    def test_change_view(self) -> None:
        """Test that the change view works for all instances."""
        for obj in self.model.objects.all():
            self.assertChangeResponse(self.get_change_view(obj), obj)


class AcmeValuesMixin:
    """Mixin that sets a few static valid ACME values."""

    # ACME data present in all mixins
    ACME_THUMBPRINT_1 = "U-yUM27CQn9pClKlEITobHB38GJOJ9YbOxnw5KKqU-8"
    ACME_THUMBPRINT_2 = "s_glgc6Fem0CW7ZioXHBeuUQVHSO-viZ3xNR8TBebCo"
    ACME_PEM_1 = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvP5N/1KjBQniyyukn30E
tyHz6cIYPv5u5zZbHGfNvrmMl8qHMmddQSv581AAFa21zueS+W8jnRI5ISxER95J
tNad2XEDsFINNvYaSG8E54IHMNQijVLR4MJchkfMAa6g1gIsJB+ffEt4Ea3TMyGr
MifJG0EjmtjkjKFbr2zuPhRX3fIGjZTlkxgvb1AY2P4AxALwS/hG4bsxHHNxHt2Z
s9Bekv+55T5+ZqvhNz1/3yADRapEn6dxHRoUhnYebqNLSVoEefM+h5k7AS48waJS
lKC17RMZfUgGE/5iMNeg9qtmgWgZOIgWDyPEpiXZEDDKeoifzwn1LO59W8c4W6L7
XwIDAQAB
-----END PUBLIC KEY-----"""
    ACME_PEM_2 = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAp8SCUVQqpTBRyryuu560
Q8cAi18Ac+iLjaSLL4gOaDEU9CpPi4l9yCGphnQFQ92YP+GWv+C6/JRp24852QbR
RzuUJqJPdDxD78yFXoxYCLPmwQMnToA7SE3SnZ/PW2GPFMbAICuRdd3PhMAWCODS
NewZPLBlG35brRlfFtUEc2oQARb2lhBkMXrpIWeuSNQtInAHtfTJNA51BzdrIT2t
MIfadw4ljk7cVbrSYemT6e59ATYxiMXalu5/4v22958voEBZ38TE8AXWiEtTQYwv
/Kj0P67yuzE94zNdT28pu+jJYr5nHusa2NCbvnYFkDwzigmwCxVt9kW3xj3gfpgc
VQIDAQAB
-----END PUBLIC KEY-----"""
    ACME_SLUG_1 = "Mr6FfdD68lzp"
    ACME_SLUG_2 = "DzW4PQ6L76PE"
