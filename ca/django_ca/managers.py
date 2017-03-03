# -*- coding: utf-8 -*-
#
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

import os
import re

from OpenSSL import crypto
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import dsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PrivateFormat
from cryptography.x509.oid import AuthorityInformationAccessOID
from cryptography.x509.oid import ExtensionOID

from django.db import models
from django.utils.encoding import force_bytes

from . import ca_settings
from .utils import NAME_OID_MAPPINGS
from .utils import SAN_OPTIONS_RE
from .utils import get_basic_cert
from .utils import get_cert_builder
from .utils import get_subjectAltName
from .utils import is_power2
from .utils import parse_general_name
from .utils import sort_subject_dict


class CertificateManagerMixin(object):
    def get_common_extensions(self, issuer_url=None, crl_url=None, ocsp_url=None):
        extensions = []

        # Set CRL distribution points:
        if crl_url:
            if isinstance(crl_url, str):
                crl_url = [url.strip() for url in crl_url.split()]
            value = force_bytes(','.join(['URI:%s' % uri for uri in crl_url]))
            extensions.append(crypto.X509Extension(b'crlDistributionPoints', 0, value))

        auth_info_access = []
        if ocsp_url:
            auth_info_access.append('OCSP;URI:%s' % ocsp_url)
        if issuer_url:
            auth_info_access.append('caIssuers;URI:%s' % issuer_url)
        if auth_info_access:
            auth_info_access = force_bytes(','.join(auth_info_access))
            extensions.append(crypto.X509Extension(b'authorityInfoAccess', 0, auth_info_access))

        return extensions

    def get_common_builder_extensions(self, issuer_url=None, crl_url=None, ocsp_url=None):
        extensions = []
        if crl_url:
            if isinstance(crl_url, str):
                crl_url = [url.strip() for url in crl_url.split()]
            urls = [x509.UniformResourceIdentifier(c) for c in crl_url]
            dps = [x509.DistributionPoint(full_name=[c], relative_name=None, crl_issuer=None, reasons=None)
                   for c in urls]
            extensions.append((False, x509.CRLDistributionPoints(dps)))
        auth_info_access = []
        if ocsp_url:
            uri = x509.UniformResourceIdentifier(ocsp_url)
            auth_info_access.append(x509.AccessDescription(
                access_method=AuthorityInformationAccessOID.OCSP, access_location=uri))
        if issuer_url:
            uri = x509.UniformResourceIdentifier(issuer_url)
            auth_info_access.append(x509.AccessDescription(
                access_method=AuthorityInformationAccessOID.CA_ISSUERS, access_location=uri))
        if auth_info_access:
            extensions.append((False, x509.AuthorityInformationAccess(auth_info_access)))
        return extensions


class CertificateAuthorityManager(CertificateManagerMixin, models.Manager):
    def init(self, name, key_size, key_type, algorithm, expires, parent, pathlen, subject,
             issuer_url=None, issuer_alt_name=None, crl_url=None, ocsp_url=None,
             ca_issuer_url=None, ca_crl_url=None, ca_ocsp_url=None, name_constraints=None,
             password=None):
        # NOTE: This is already verified by KeySizeAction, so none of these checks should ever be
        #       True in the real world. None the less they are here as a safety precaution.
        if not is_power2(key_size):
            raise RuntimeError("%s: Key size must be a power of two." % key_size)
        elif key_size < ca_settings.CA_MIN_KEY_SIZE:
            raise RuntimeError("%s: Key size must be least %s bits."
                               % (key_size, ca_settings.CA_MIN_KEY_SIZE))

        try:
            algorithm = getattr(hashes, algorithm.upper())
        except AttributeError:
            raise RuntimeError('Unknown algorithm specified: %s' % algorithm)

        if key_type == 'DSA':
            private_key = dsa.generate_private_key(key_size=key_size, backend=default_backend())
        else:
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size,
                                                   backend=default_backend())
        public_key = private_key.public_key()

        builder = get_cert_builder(expires)
        builder = builder.public_key(public_key)

        # Set subject (order is important!)
        subject = [x509.NameAttribute(NAME_OID_MAPPINGS[k], v)
                   for k, v in sort_subject_dict(subject)]
        builder = builder.subject_name(x509.Name(subject))

        # TODO: pathlen=None is currently False :/
        if pathlen is False:
            pathlen = None

        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=pathlen), critical=True)
        builder = builder.add_extension(x509.KeyUsage(
            key_cert_sign=True, crl_sign=True, digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False,
            decipher_only=False), critical=True)

        subject_key_id = x509.SubjectKeyIdentifier.from_public_key(public_key)
        builder = builder.add_extension(subject_key_id, critical=False)

        if parent is None:
            builder = builder.issuer_name(x509.Name(subject))
            auth_key_id = x509.AuthorityKeyIdentifier(
                key_identifier=subject_key_id.digest, authority_cert_issuer=None,
                authority_cert_serial_number=None)
        else:
            builder = builder.issuer_name(parent.x509c.subject)
            auth_key_id = parent.x509c.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_KEY_IDENTIFIER)

        builder = builder.add_extension(auth_key_id, critical=False)

        for critical, ext in self.get_common_builder_extensions(ca_issuer_url, ca_crl_url, ca_ocsp_url):
            builder = builder.add_extension(ext, critical=critical)

        # TODO: pass separate lists maybe?
        if name_constraints is not None:
            excluded = []
            permitted = []
            for constraint in name_constraints:
                typ, name = constraint.split(';', 1)
                parsed = parse_general_name(name)
                if typ == 'permitted':
                    permitted.append(parsed)
                else:
                    excluded.append(parsed)

            builder = builder.add_extension(x509.NameConstraints(
                permitted_subtrees=permitted, excluded_subtrees=excluded), critical=True)

        certificate = builder.sign(
            private_key=private_key, algorithm=algorithm(),
            backend=default_backend()
        )

        if crl_url is not None:
            crl_url = '\n'.join(crl_url)

        ca = self.model(name=name, issuer_url=issuer_url, issuer_alt_name=issuer_alt_name,
                        ocsp_url=ocsp_url, crl_url=crl_url, parent=parent)
        ca.x509c = certificate
        ca.private_key_path = os.path.join(ca_settings.CA_DIR, '%s.key' % ca.serial)
        ca.save()

        # write private key to file
        oldmask = os.umask(247)
        pem = private_key.private_bytes(encoding=Encoding.PEM,
                                        format=PrivateFormat.TraditionalOpenSSL,
                                        encryption_algorithm=serialization.NoEncryption())
        with open(ca.private_key_path, 'wb') as key_file:
            key_file.write(pem)
        os.umask(oldmask)

        return ca

    def _init(self, name, key_size, key_type, algorithm, expires, parent, pathlen, subject,
              issuer_url=None, issuer_alt_name=None, crl_url=None, ocsp_url=None,
              ca_issuer_url=None, ca_crl_url=None, ca_ocsp_url=None,
              name_constraints=None, password=None):
        """Create a Certificate Authority."""

        # NOTE: This is already verified by KeySizeAction, so none of these checks should ever be
        #       True in the real world. None the less they are here as a safety precaution.
        if not is_power2(key_size):
            raise RuntimeError("%s: Key size must be a power of two." % key_size)
        elif key_size < ca_settings.CA_MIN_KEY_SIZE:
            raise RuntimeError("%s: Key size must be least %s bits."
                               % (key_size, ca_settings.CA_MIN_KEY_SIZE))

        private_key = crypto.PKey()
        private_key.generate_key(getattr(crypto, 'TYPE_%s' % key_type), key_size)

        # set basic properties
        cert = get_basic_cert(expires)
        for key, value in sort_subject_dict(subject):
            setattr(cert.get_subject(), key, force_bytes(value))
        cert.set_pubkey(private_key)

        basicConstraints = 'CA:TRUE'
        if pathlen is not False:
            basicConstraints += ', pathlen:%s' % pathlen

        cert.add_extensions([
            crypto.X509Extension(b'basicConstraints', True, basicConstraints.encode('utf-8')),
            crypto.X509Extension(b'keyUsage', 0, b'keyCertSign,cRLSign'),
            crypto.X509Extension(b'subjectKeyIdentifier', False, b'hash', subject=cert),
        ])

        extensions = self.get_common_extensions(ca_issuer_url, ca_crl_url, ca_ocsp_url)

        if name_constraints:
            name_constraints = ','.join(name_constraints).encode('utf-8')
            extensions.append(crypto.X509Extension(b'nameConstraints', True, name_constraints))

        if parent is None:
            cert.set_issuer(cert.get_subject())
            extensions.append(crypto.X509Extension(b'authorityKeyIdentifier', False,
                                                   b'keyid:always', issuer=cert))
        else:
            cert.set_issuer(parent.x509.get_subject())
            extensions.append(crypto.X509Extension(b'authorityKeyIdentifier', False,
                                                   b'keyid,issuer', issuer=parent.x509))
        cert.add_extensions(extensions)

        # sign the certificate
        if parent is None:
            cert.sign(private_key, algorithm)
        else:
            cert.sign(parent.key, algorithm)

        if crl_url is not None:
            crl_url = '\n'.join(crl_url)

        # create certificate in database
        ca = self.model(name=name, issuer_url=issuer_url, issuer_alt_name=issuer_alt_name,
                        ocsp_url=ocsp_url, crl_url=crl_url, parent=parent)
        ca.x509 = cert
        ca.private_key_path = os.path.join(ca_settings.CA_DIR, '%s.key' % ca.serial)
        ca.save()

        dump_args = []
        if password is not None:  # pragma: no cover
            dump_args = ['des3', password]

        # write private key to file
        oldmask = os.umask(247)
        with open(ca.private_key_path, 'w') as key_file:
            key = crypto.dump_privatekey(crypto.FILETYPE_PEM, private_key, *dump_args)
            key_file.write(key.decode('utf-8'))
        os.umask(oldmask)

        return ca


class CertificateManager(CertificateManagerMixin, models.Manager):
    def init(self, ca, csr, expires, algorithm, subject=None, cn_in_san=True,
             csr_format=crypto.FILETYPE_PEM, subjectAltName=None, keyUsage=None,
             extendedKeyUsage=None, tlsfeature=None):
        """Create a signed certificate from a CSR.

        X509 extensions (`key_usage`, `ext_key_usage`) may either be None (in which case they are
        not added) or a tuple with the first value being a bool indicating if the value is critical
        and the second value being a byte-array indicating the extension value. Example::

            (True, b'value')

        Parameters
        ----------

        ca : django_ca.models.CertificateAuthority
            The certificate authority to sign the certificate with.
        csr : str
            A valid CSR in PEM format. If none is given, `self.csr` will be used.
        expires : int
            When the certificate should expire (passed to :py:func:`get_basic_cert`).
        algorithm : {'sha512', 'sha256', ...}
            Algorithm used to sign the certificate. The default is the CA_DIGEST_ALGORITHM setting.
        subject : dict, optional
            The Subject to use in the certificate.  The keys of this dict are the fields of an X509
            subject, that is `"C"`, `"ST"`, `"L"`, `"OU"` and `"CN"`. If ommited or if the value
            does not contain a `"CN"` key, the first value of the `subjectAltName` parameter is
            used as CommonName (and is obviously mandatory in this case).
        cn_in_san : bool, optional
            Wether the CommonName should also be included as subjectAlternativeName. The default is
            `True`, but the parameter is ignored if no CommonName is given. This is typically set
            to `False` when creating a client certificate, where the subjects CommonName has no
            meaningful value as subjectAltName.
        csr_format : int, optional
            The format of the submitted CSR request. One of the OpenSSL.crypto.FILETYPE_*
            constants. The default is PEM.
        subjectAltName : list of str, optional
            A list of values for the subjectAltName extension. Values are passed to
            `get_subjectAltName`, see function documentation for how this value is parsed.
        keyUsage : tuple or None
            Value for the `keyUsage` X509 extension. See description for format details.
        extendedKeyUsage : tuple or None
            Value for the `extendedKeyUsage` X509 extension. See description for format details.
        tlsfeature : tuple or None
            Value for the `tlsfeature` extension. See description for format details.

        Returns
        -------

        OpenSSL.crypto.X509
            The signed certificate.
        """
        if subject is None:
            subject = {}
        if not subject.get('CN') and not subjectAltName:
            raise ValueError("Must at least cn or subjectAltName parameter.")

        req = crypto.load_certificate_request(csr_format, csr)

        # Process CommonName and subjectAltName extension.
        if subject.get('CN') is None:
            subject['CN'] = re.sub('^%s' % SAN_OPTIONS_RE, '', subjectAltName[0])
            subjectAltName = get_subjectAltName(subjectAltName)
        elif cn_in_san is True:
            if subjectAltName:
                subjectAltName = get_subjectAltName(subjectAltName, cn=subject['CN'])
            else:
                subjectAltName = get_subjectAltName([subject['CN']])

        # subjectAltName might still be None, in which case the extension is not added.
        elif subjectAltName:
            subjectAltName = get_subjectAltName(subjectAltName)

        # Create signed certificate
        cert = get_basic_cert(expires)
        cert.set_issuer(ca.x509.get_subject())
        for key, value in sort_subject_dict(subject):
            setattr(cert.get_subject(), key, force_bytes(value))
        cert.set_pubkey(req.get_pubkey())

        extensions = self.get_common_extensions(ca.issuer_url, ca.crl_url, ca.ocsp_url)
        extensions += [
            crypto.X509Extension(b'subjectKeyIdentifier', 0, b'hash', subject=cert),
            crypto.X509Extension(b'authorityKeyIdentifier', 0, b'keyid,issuer', issuer=ca.x509),
            crypto.X509Extension(b'basicConstraints', True, b'CA:FALSE'),
        ]

        if keyUsage is not None:
            extensions.append(crypto.X509Extension(b'keyUsage', *keyUsage))
        if extendedKeyUsage is not None:
            extensions.append(crypto.X509Extension(b'extendedKeyUsage', *extendedKeyUsage))

        if tlsfeature is not None:  # pragma: no cover
            extensions.append(crypto.X509Extension(b'tlsFeature', *tlsfeature))

        # Add subjectAltNames, always also contains the CommonName
        if subjectAltName:
            extensions.append(crypto.X509Extension(b'subjectAltName', 0, subjectAltName))

        # Add issuerAltName
        if ca.issuer_alt_name:
            issuerAltName = force_bytes('URI:%s' % ca.issuer_alt_name)
        else:
            issuerAltName = b'issuer:copy'
        extensions.append(crypto.X509Extension(b'issuerAltName', 0, issuerAltName, issuer=ca.x509))

        # Add collected extensions
        cert.add_extensions(extensions)

        # Finally sign the certificate:
        cert.sign(ca.key, str(algorithm))  # str() to force py2 unicode to str

        return cert
