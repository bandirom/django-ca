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

"""Test ACME certificate revocation."""

import typing
from http import HTTPStatus

import acme
import josepy as jose
from OpenSSL.crypto import X509
from OpenSSL.crypto import X509Req

from django.http import HttpResponse
from django.test import TestCase

from freezegun import freeze_time

from .... import ca_settings
from ....constants import ReasonFlags
from ....models import AcmeAccount
from ....models import AcmeAuthorization
from ....models import AcmeCertificate
from ....models import AcmeOrder
from ....models import Certificate
from ....utils import get_cert_builder
from ...base import certs
from ...base import override_tmpcadir
from ...base import timestamps
from ...tests_views_acme import AcmeWithAccountViewTestCaseMixin


@freeze_time(timestamps["everything_valid"])
class AcmeCertificateRevocationViewTestCase(
    AcmeWithAccountViewTestCaseMixin[acme.messages.Revocation], TestCase
):
    """Test revoking a certificate."""

    message_cls = acme.messages.Revocation
    view_name = "acme-revoke"
    load_cas = ("root", "child")
    load_certs = ("root-cert", "child-cert")

    class csr_class(acme.messages.Revocation):  # pylint: disable=invalid-name
        """Class that allows us to send a CSR in the certificate field for testing."""

        certificate = jose.json_util.Field(  # type: ignore[assignment]
            "certificate", decoder=jose.json_util.decode_csr, encoder=jose.json_util.encode_csr
        )

    def setUp(self) -> None:
        super().setUp()
        self.order = AcmeOrder.objects.create(account=self.account, status=AcmeOrder.STATUS_VALID)
        self.acmecert = AcmeCertificate.objects.create(order=self.order, cert=self.cert)

    @property
    def url(self) -> str:
        """Get URL for the standard auth object."""
        return self.get_url(serial=self.ca.serial)

    def get_message(self, **kwargs: typing.Any) -> acme.messages.Revocation:
        kwargs.setdefault(
            "certificate", jose.util.ComparableX509(X509.from_cryptography(self.cert.pub.loaded))
        )
        return self.message_cls(**kwargs)

    @override_tmpcadir()
    def test_wrong_jwk_or_kid(self) -> None:
        """This test makes no sense here, as we accept both JWK and JID."""

    def test_basic(self) -> None:
        """Test a very basic certificate revocation."""
        resp = self.acme(self.url, self.message, kid=self.kid)
        self.assertEqual(resp.status_code, HTTPStatus.OK, resp.content)

        self.cert.refresh_from_db()
        self.assertTrue(self.cert.revoked)
        self.assertEqual(self.cert.revoked_date, timestamps["everything_valid"])
        self.assertEqual(self.cert.revoked_reason, ReasonFlags.unspecified.value)

    def test_reason_code(self) -> None:
        """Test revocation reason."""
        message = self.get_message(reason=3)
        resp = self.acme(self.url, message, kid=self.kid)
        self.assertEqual(resp.status_code, HTTPStatus.OK, resp.content)

        self.cert.refresh_from_db()
        self.assertTrue(self.cert.revoked)
        self.assertEqual(self.cert.revoked_date, timestamps["everything_valid"])
        self.assertEqual(self.cert.revoked_reason, ReasonFlags.affiliation_changed.name)

    def test_already_revoked(self) -> None:
        """Test revoking a certificate that is already revoked."""
        resp = self.acme(self.url, self.message, kid=self.kid)
        self.assertEqual(resp.status_code, HTTPStatus.OK, resp.content)

        resp = self.acme(self.url, self.message, kid=self.kid)
        self.assertMalformed(resp, "Certificate was already revoked.", typ="alreadyRevoked")

    def test_bad_reason_code(self) -> None:
        """Send a bad revocation reason code to the server."""
        message = self.get_message(reason=99)
        resp = self.acme(self.url, message, kid=self.kid)
        self.assertMalformed(resp, "99: Unsupported revocation reason.", typ="badRevocationReason")

        self.cert.refresh_from_db()
        self.assertFalse(self.cert.revoked)

    def test_unknown_certificate(self) -> None:
        """Try sending an unknown certificate to the server."""
        Certificate.objects.all().delete()
        resp = self.acme(self.url, self.message, kid=self.kid)
        self.assertUnauthorized(resp, "Certificate not found.")

    @override_tmpcadir()
    def test_wrong_certificate(self) -> None:
        """Test sending a different certificate with the same serial."""

        # Create a clone of the existing certificate with the same serial number
        pkey = certs["root-cert"]["csr"]["parsed"].public_key()
        builder = get_cert_builder(self.cert.expires, serial=self.cert.pub.loaded.serial_number)
        builder = builder.public_key(pkey)
        builder = builder.issuer_name(self.ca.subject)
        builder = builder.subject_name(self.cert.pub.loaded.subject)
        cert = builder.sign(private_key=self.ca.key(), algorithm=ca_settings.CA_DIGEST_ALGORITHM)
        message = self.message_cls(certificate=jose.util.ComparableX509(X509.from_cryptography(cert)))

        resp = self.acme(self.url, message, kid=self.kid)
        self.assertUnauthorized(resp, "Certificate does not match records.")

    def test_pass_csr(self) -> None:
        """Send a CSR instead of a certificate."""
        req = X509Req.from_cryptography(certs["root-cert"]["csr"]["parsed"])
        message = self.csr_class(certificate=jose.util.ComparableX509(req))
        resp = self.acme(self.url, message, kid=self.kid)
        self.assertMalformed(resp, "Could not decode 'certificate'", regex=True)


@freeze_time(timestamps["everything_valid"])
class AcmeCertificateRevocationWithAuthorizationsViewTestCase(AcmeCertificateRevocationViewTestCase):
    """Test certificate revocation by signing the request with the compromised certificate."""

    def setUp(self) -> None:
        super().setUp()

        self.acme_order = AcmeOrder.objects.create(account=self.main_account)
        self.acme_auth = AcmeAuthorization.objects.create(
            order=self.acme_order, value="child-cert.example.com", status=AcmeAuthorization.STATUS_VALID
        )

    def acme(self, *args: typing.Any, **kwargs: typing.Any) -> HttpResponse:
        kwargs.setdefault("cert", certs["child-cert"]["key"]["parsed"])
        kwargs["kid"] = self.child_kid
        return super().acme(*args, **kwargs)

    @property
    def main_account(self) -> AcmeAccount:
        return self.account2

    def test_unknown_account(self) -> None:
        pass

    def test_wrong_authorizations(self) -> None:
        """Test revoking a certificate when the account has some, but the wrong authorizations."""
        self.acme_auth.value = "wrong.example.com"
        self.acme_auth.save()

        resp = self.acme(self.url, self.get_message())
        self.assertUnauthorized(resp, "Account does not hold necessary authorizations.")

    def test_no_extensions(self) -> None:
        """Test revoking a certificate that has no SubjectAltName extension."""
        cert = self.load_named_cert("no-extensions")

        # Create AcmeOrder/Certificate (only certs issued via ACME can be revoked via ACME).
        order = AcmeOrder.objects.create(account=self.account, status=AcmeOrder.STATUS_VALID)
        AcmeCertificate.objects.create(order=order, cert=cert)

        message_cert = jose.util.ComparableX509(X509.from_cryptography(cert.pub.loaded))
        resp = self.acme(self.url, self.get_message(certificate=message_cert))
        self.assertUnauthorized(resp, "Account does not hold necessary authorizations.")

    def test_non_dns_sans(self) -> None:
        """Test revoking a certificate that has no SubjectAltName extension."""
        cert = self.load_named_cert("alt-extensions")

        # Create AcmeOrder/Certificate (only certs issued via ACME can be revoked via ACME).
        order = AcmeOrder.objects.create(account=self.account, status=AcmeOrder.STATUS_VALID)
        AcmeCertificate.objects.create(order=order, cert=cert)

        message_cert = jose.util.ComparableX509(X509.from_cryptography(cert.pub.loaded))
        resp = self.acme(self.url, self.get_message(certificate=message_cert))
        self.assertUnauthorized(resp, "Certificate contains non-DNS subjectAlternativeNames.")


@freeze_time(timestamps["everything_valid"])
class AcmeCertificateRevocationWithJWKViewTestCase(AcmeCertificateRevocationViewTestCase):
    """Test certificate revocation by signing the request with the compromised certificate."""

    requires_kid = False

    def acme(self, *args: typing.Any, **kwargs: typing.Any) -> HttpResponse:
        kwargs.setdefault("cert", certs[self.default_cert]["key"]["parsed"])
        kwargs["kid"] = None
        return super().acme(*args, **kwargs)

    def test_wrong_signer(self) -> None:
        """Sign the request with the wrong certificate."""
        cert = certs["root-cert"]["key"]["parsed"]
        resp = self.acme(self.url, self.message, cert=cert)
        self.assertUnauthorized(resp, "Request signed by the wrong certificate.")

    def test_deactivated_account(self) -> None:
        """Not applicable: Certificate-signed revocation requests do not require a valid account."""

    def test_unknown_account(self) -> None:
        """Not applicable: Certificate-signed revocation requests do not require a valid account."""

    def test_unusable_account(self) -> None:
        """Not applicable: Certificate-signed revocation requests do not require a valid account."""

    def test_jwk_and_kid(self) -> None:
        """Already tested in the immediate base class and does not make sense here: The test sets KID to a
        value, but the point of the whole class is to have *no* KID."""