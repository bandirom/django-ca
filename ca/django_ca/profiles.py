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

"""Module for handling certificate profiles."""

from copy import deepcopy
from datetime import timedelta
from threading import local
from typing import TYPE_CHECKING
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union
from typing import cast

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from . import ca_settings
from .extensions import KEY_TO_EXTENSION
from .extensions import AuthorityInformationAccess
from .extensions import AuthorityKeyIdentifier
from .extensions import BasicConstraints
from .extensions import CRLDistributionPoints
from .extensions import Extension
from .extensions import IssuerAlternativeName
from .extensions import SubjectAlternativeName
from .extensions import SubjectKeyIdentifier
from .extensions.utils import DistributionPoint
from .signals import pre_issue_cert
from .subject import Subject
from .typehints import Expires
from .typehints import SerializedProfile
from .utils import get_cert_builder
from .utils import parse_expires
from .utils import parse_general_name
from .utils import parse_hash_algorithm
from .utils import shlex_split

if TYPE_CHECKING:
    from .models import Certificate
    from .models import CertificateAuthority


class Profile:
    """A certificate profile defining properties and extensions of a certificate.

    Instances of this class usually represent profiles defined in :ref:`CA_PROFILES <settings-ca-profiles>`,
    but you can also create your own profile to create a different type of certificate. An instance of this
    class can be used to create a signed certificate based on the given CA::

        >>> Profile('example', subject='/C=AT', extensions={'ocsp_no_check': {}})
        <Profile: 'example'>
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        name,
        subject=None,
        algorithm=None,
        extensions=None,
        cn_in_san: bool = True,
        expires: Optional[Union[int, timedelta]] = None,
        issuer_name=None,
        description="",
        autogenerated=False,
        add_crl_url=True,
        add_ocsp_url=True,
        add_issuer_url=True,
        add_issuer_alternative_name=True,
    ) -> None:
        # pylint: disable=too-many-locals,too-many-arguments
        self.name = name

        if isinstance(expires, int):
            expires = timedelta(days=expires)

        # self.subject is default subject with updates from subject argument
        if subject is not None:
            if not isinstance(subject, Subject):
                subject = Subject(subject)  # NOTE: also accepts None
            self.subject = subject
        else:
            self.subject = Subject(ca_settings.CA_DEFAULT_SUBJECT)

        self.algorithm = parse_hash_algorithm(algorithm)
        self.extensions = deepcopy(extensions) or {}
        self.cn_in_san = cn_in_san
        self.expires = expires or ca_settings.CA_DEFAULT_EXPIRES
        self.issuer_name = issuer_name
        self.add_crl_url = add_crl_url
        self.add_issuer_url = add_issuer_url
        self.add_ocsp_url = add_ocsp_url
        self.add_issuer_alternative_name = add_issuer_alternative_name
        self.description = description
        self.autogenerated = autogenerated

        # cast extensions to their respective classes
        for key, extension in self.extensions.items():
            if not isinstance(extension, Extension):
                self.extensions[key] = KEY_TO_EXTENSION[key](extension)

        # set some sane extension defaults
        self.extensions.setdefault(BasicConstraints.key, BasicConstraints())

    def __eq__(self, o: object) -> bool:
        if isinstance(o, (Profile, DefaultProfileProxy)) is False:
            return False
        algo = isinstance(o.algorithm, type(self.algorithm))

        return (
            self.name == o.name
            and self.subject == o.subject
            and algo
            and self.extensions == o.extensions
            and self.cn_in_san == o.cn_in_san
            and self.expires == o.expires
            and self.issuer_name == o.issuer_name
            and self.add_crl_url == o.add_crl_url
            and self.add_issuer_url == o.add_issuer_url
            and self.add_ocsp_url == o.add_ocsp_url
            and self.add_issuer_alternative_name == o.add_issuer_alternative_name
            and self.description == o.description
        )

    def _parse_extension_value(self, key, value):
        """Parse an extension value into a django_ca extension."""

        if isinstance(value, Extension):
            return value

        if value is None:
            return None

        return KEY_TO_EXTENSION[key](value)

    def __repr__(self) -> str:
        return "<Profile: %r>" % self.name

    def __str__(self) -> str:
        return repr(self)

    def create_cert(
        self,
        ca: "CertificateAuthority",
        csr,
        subject=None,
        expires: Expires = None,
        algorithm=None,
        extensions=None,
        cn_in_san: Optional[bool] = None,
        add_crl_url=None,
        add_ocsp_url=None,
        add_issuer_url=None,
        add_issuer_alternative_name=None,
        password=None,
    ) -> x509.Certificate:
        """Create a x509 certificate based on this profile, the passed CA and input parameters.

        This function is the core function used to create x509 certificates. In it's simplest form, you only
        need to pass a ca, a CSR and a subject to get a valid certificate::

            >>> profile = get_profile('webserver')
            >>> profile.create_cert(ca, csr, subject='/CN=example.com')  # doctest: +ELLIPSIS
            <Certificate(subject=<Name(...,CN=example.com)>, ...)>

        The function will add CRL, OCSP, Issuer and IssuerAlternativeName URLs based on the CA if the profile
        has the *add_crl_url*, *add_ocsp_url* and *add_issuer_url* and *add_issuer_alternative_name* values
        set. Parameters to this function with the same name allow you override this behavior.

        The function allows you to override profile values using the *expires* and *algorithm* values. You can
        pass additional *extensions* as a list, which will override any extensions from the profile, but the
        CA passed will append to these extensions unless the *add_...* values are ``False``.

        Parameters
        ----------

        ca : :py:class:`~django_ca.models.CertificateAuthority`
            The CA to sign the certificate with.
        csr : str or :py:class:`~cg:cryptography.x509.CertificateSigningRequest`
            The CSR for the certificate.
        subject : dict or str or :py:class:`~django_ca.subject.Subject`
            Update the subject string, e.g. ``"/CN=example.com"`` or ``Subject("/CN=example.com")``. The
            values from the passed subject will update the profiles subject.
        expires : int or datetime or timedelta, optional
            Override when this certificate will expire.
        algorithm : str or :py:class:`~cg:cryptography.hazmat.primitives.hashes.HashAlgorithm`, optional
            Override the hash algorithm used when signing the certificate, passed to
            :py:func:`~django_ca.utils.parse_hash_algorithm`.
        extensions : list or dict of :py:class:`~django_ca.extensions.base.Extension`
            List or dict of additional extensions to set for the certificate. Note that values from the CA
            might update the passed extensions: For example, if you pass an
            :py:class:`~django_ca.extensions.IssuerAlternativeName` extension, *add_issuer_alternative_name*
            is ``True`` and the passed CA has an IssuerAlternativeName set, that value will be appended to the
            extension you pass here. If you pass a dict with a ``None`` value, that extension will be removed
            from the profile.
        cn_in_san : bool, optional
            Override if the CommonName should be added as an SubjectAlternativeName. If not passed, the value
            set in the profile is used.
        add_crl_url : bool, optional
            Override if any CRL URLs from the CA should be added to the CA. If not passed, the value set in
            the profile is used.
        add_ocsp_url : bool, optional
            Override if any OCSP URLs from the CA should be added to the CA. If not passed, the value set in
            the profile is used.
        add_issuer_url : bool, optional
            Override if any Issuer URLs from the CA should be added to the CA. If not passed, the value set in
            the profile is used.
        add_issuer_alternative_name : bool, optional
            Override if any IssuerAlternativeNames from the CA should be added to the CA. If not passed, the
            value set in the profile is used.
        password: bytes or str, optional
            The password to the private key of the CA.

        Returns
        -------

        cryptography.x509.Certificate
            The signed certificate.
        """
        # pylint: disable=too-many-locals,too-many-arguments

        # Compute default values
        if extensions is None:
            extensions = {}
        elif isinstance(extensions, dict):
            extensions = {k: self._parse_extension_value(k, v) for k, v in extensions.items()}
        else:
            extensions = {e.key: e for e in extensions}

        # Get overrides values from profile if not passed as parameter
        if cn_in_san is None:
            cn_in_san = self.cn_in_san
        if add_crl_url is None:
            add_crl_url = self.add_crl_url
        if add_ocsp_url is None:
            add_ocsp_url = self.add_ocsp_url
        if add_issuer_url is None:
            add_issuer_url = self.add_issuer_url
        if add_issuer_alternative_name is None:
            add_issuer_alternative_name = self.add_issuer_alternative_name

        cert_extensions = deepcopy(self.extensions)
        cert_extensions.update(extensions)
        cert_extensions = {k: v for k, v in cert_extensions.items() if v is not None}
        cert_subject = deepcopy(self.subject)

        issuer_name = self._update_from_ca(
            ca,
            cert_extensions,
            add_crl_url=add_crl_url,
            add_ocsp_url=add_ocsp_url,
            add_issuer_url=add_issuer_url,
            add_issuer_alternative_name=add_issuer_alternative_name,
        )

        if not isinstance(subject, Subject):
            subject = Subject(subject)  # NOTE: also accepts None
        cert_subject.update(subject)

        if expires is None:
            expires = self.expires
        if algorithm is None:
            algorithm = self.algorithm
        else:
            algorithm = parse_hash_algorithm(algorithm)

        # Make sure that expires is a fixed timestamp
        expires = parse_expires(expires)

        # Finally, update SAN with the current CN, if set and requested
        self._update_san_from_cn(cn_in_san, subject=cert_subject, extensions=cert_extensions)

        if not subject.get("CN") and (
            SubjectAlternativeName.key not in extensions or not extensions[SubjectAlternativeName.key].value
        ):
            raise ValueError("Must name at least a CN or a subjectAlternativeName.")

        pre_issue_cert.send(
            sender=self.__class__,
            ca=ca,
            csr=csr,
            expires=expires,
            algorithm=algorithm,
            subject=cert_subject,
            extensions=cert_extensions,
            password=password,
        )

        public_key = csr.public_key()
        builder = get_cert_builder(expires)
        builder = builder.public_key(public_key)
        builder = builder.issuer_name(issuer_name)
        builder = builder.subject_name(cert_subject.name)

        for _key, extension in cert_extensions.items():
            builder = builder.add_extension(*extension.for_builder())

        # Add the SubjectKeyIdentifier
        if SubjectKeyIdentifier.key not in cert_extensions:
            builder = builder.add_extension(
                x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
            )

        return builder.sign(private_key=ca.key(password), algorithm=algorithm, backend=default_backend())

    def copy(self) -> "Profile":
        """Create a deep copy of a profile."""
        return deepcopy(self)

    def serialize(self) -> SerializedProfile:
        """Function to serialize a profile.

        This is function is called by the admin interface to retrieve profile information to the browser, so
        the value returned by this function should always be JSON serializable.
        """
        data = {
            "cn_in_san": self.cn_in_san,
            "description": self.description,
            "subject": dict(self.subject),
            "extensions": {k: e.serialize() for k, e in self.extensions.items()},
        }

        return data

    def _update_from_ca(
        self,
        ca: "CertificateAuthority",
        extensions: Dict[str, Extension[Any, Any, Any]],
        add_crl_url: bool,
        add_ocsp_url: bool,
        add_issuer_url: bool,
        add_issuer_alternative_name: bool,
    ) -> x509.Name:
        """Update data from the given CA.

        * Sets the AuthorityKeyIdentifier extension
        * Sets the OCSP url if add_ocsp_url is True
        * Sets a CRL URL if add_crl_url is True
        * Adds an IssuerAlternativeName if add_issuer_alternative_name is True

        """
        extensions.setdefault(AuthorityKeyIdentifier.key, ca.get_authority_key_identifier_extension())

        if add_crl_url is not False and ca.crl_url:
            extensions.setdefault(CRLDistributionPoints.key, CRLDistributionPoints())
            extensions[CRLDistributionPoints.key].value.append(
                DistributionPoint(
                    {
                        "full_name": [url.strip() for url in ca.crl_url.split()],
                    }
                )
            )

        if add_ocsp_url is not False and ca.ocsp_url:
            extensions.setdefault(AuthorityInformationAccess.key, AuthorityInformationAccess())
            extensions[AuthorityInformationAccess.key].ocsp.append(parse_general_name(ca.ocsp_url))

        if add_issuer_url is not False and ca.issuer_url:
            extensions.setdefault(AuthorityInformationAccess.key, AuthorityInformationAccess())
            extensions[AuthorityInformationAccess.key].issuers.append(parse_general_name(ca.issuer_url))
        if add_issuer_alternative_name is not False and ca.issuer_alt_name:
            extensions.setdefault(IssuerAlternativeName.key, IssuerAlternativeName())
            extensions[IssuerAlternativeName.key].extend(shlex_split(ca.issuer_alt_name, ","))

        if self.issuer_name:
            return self.issuer_name.name

        return ca.x509_cert.subject

    def _update_san_from_cn(
        self,
        cn_in_san: bool,
        subject: Subject,
        extensions: Dict[str, Extension[Any, Any, Any]],
    ) -> None:
        if subject.get("CN") and cn_in_san is True:
            try:
                common_name = parse_general_name(cast(str, subject["CN"]))
            except ValueError as e:
                raise ValueError(
                    "%s: Could not parse CommonName as subjectAlternativeName." % subject["CN"]
                ) from e

            extensions.setdefault(SubjectAlternativeName.key, SubjectAlternativeName())
            san_ext = cast(SubjectAlternativeName, extensions[SubjectAlternativeName.key])
            if common_name not in san_ext:
                san_ext.append(common_name)
        elif not subject.get("CN") and SubjectAlternativeName.key in extensions:
            san_ext = cast(SubjectAlternativeName, extensions[SubjectAlternativeName.key])
            cn_from_san = san_ext.get_common_name()
            if cn_from_san is not None:
                subject["CN"] = cn_from_san


def get_profile(name: Optional[str] = None) -> Profile:
    """Get profile by the given name.

    Raises ``KeyError`` if the profile is not defined.

    Parameters
    ----------

    name : str, optional
        The name of the profile. If ``None``, the profile configured by
        :ref:`CA_DEFAULT_PROFILE <settings-ca-default-profile>` is used.
    """
    if name is None:
        name = ca_settings.CA_DEFAULT_PROFILE
    return Profile(name, **ca_settings.CA_PROFILES[name])


class Profiles:  # pylint: disable=too-few-public-methods
    """A profile handler similar to Djangos CacheHandler."""

    def __init__(self) -> None:
        self._profiles = local()

    def __getitem__(self, name: Optional[str]) -> Profile:
        if name is None:
            name = ca_settings.CA_DEFAULT_PROFILE

        try:
            return cast(Profile, self._profiles.profiles[name])
        except AttributeError:
            self._profiles.profiles = {}
        except KeyError:
            pass

        self._profiles.profiles[name] = get_profile(name)
        return cast(Profile, self._profiles.profiles[name])

    def _reset(self) -> None:
        self._profiles = local()


profiles = Profiles()


class DefaultProfileProxy:
    """Default profile proxy, similar to Djangos DefaultCacheProxy.

    .. NOTE:: We don't implement setattr/delattr, because Profiles are supposed to be read-only anyway.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(profiles[ca_settings.CA_DEFAULT_PROFILE], name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (DefaultProfileProxy, Profile)):
            return False
        return profiles[ca_settings.CA_DEFAULT_PROFILE] == other

    def __repr__(self) -> str:
        return "<DefaultProfile: %r>" % self.name

    def __str__(self) -> str:
        return repr(self)


profile = DefaultProfileProxy()
