"""Makes the certificates the intercepting proxy shows to the tests, signed by a CA only this machine trusts."""

import datetime
import ipaddress
import os
import shutil
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Self

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class CertificateAuthority:
    """A private CA in one folder. It signs a certificate for every host the proxy pretends to be."""

    _KEY_FILE = "ca-key.pem"
    _CERT_FILE = "ca-cert.pem"
    _VALID = datetime.timedelta(days=825)

    def __init__(self, folder: Path, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> None:
        """Keep the CA key and certificate. Use `load_or_create` to get one."""
        self._folder = folder
        self._key = key
        self._cert = cert
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    @classmethod
    def load_or_create(cls, folder: Path) -> Self:
        """Read the CA from the folder, or make a new one there the first time.

        Returns:
            The CA.

        Raises:
            ValueError: If the stored key is not an RSA key.
        """
        key_path, cert_path = folder / cls._KEY_FILE, folder / cls._CERT_FILE
        if key_path.exists() and cert_path.exists():
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError(f"{key_path} does not hold an RSA key")
            return cls(folder, key, x509.load_pem_x509_certificate(cert_path.read_bytes()))
        folder.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lesstests recording CA")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + cls._VALID)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        cls._write_key(key_path, key)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cls(folder, key, cert)

    @property
    def ca_pem(self) -> Path:
        """The CA certificate as a PEM file, for anything that has to trust the proxy."""
        return self._folder / self._CERT_FILE

    def server_context(self, host: str) -> ssl.SSLContext:
        """Give a server side TLS context that shows a certificate for this host.

        Returns:
            The context, made once per host and then reused.
        """
        with self._lock:
            if host not in self._contexts:
                self._contexts[host] = self._make_context(host)
            return self._contexts[host]

    def java_truststore(self, path: Path, password: str) -> Path:
        """Write a PKCS12 truststore with the CA in it, unless the file already exists.

        Returns:
            The truststore path.

        Raises:
            RuntimeError: If keytool is missing or fails.
        """
        if path.exists():
            return path
        keytool = shutil.which("keytool")
        if keytool is None:
            raise RuntimeError("keytool is not on the PATH, it comes with the JDK")
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            keytool,
            "-importcert",
            "-noprompt",
            "-alias",
            "lesstests-ca",
            "-file",
            str(self.ca_pem),
            "-keystore",
            str(path),
            "-storetype",
            "PKCS12",
            "-storepass",
            password,
        ]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"keytool failed:\n{result.stdout[-2000:]}{result.stderr[-2000:]}")
        return path

    def _make_context(self, host: str) -> ssl.SSLContext:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + self._VALID)
            .add_extension(x509.SubjectAlternativeName([self._alt_name(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )

        # The ssl module only loads a certificate chain from files, so each host gets its own pair on disk.
        hosts = self._folder / "hosts"
        hosts.mkdir(exist_ok=True)
        cert_path, key_path = hosts / f"{host}-cert.pem", hosts / f"{host}-key.pem"
        chain = cert.public_bytes(serialization.Encoding.PEM) + self._cert.public_bytes(serialization.Encoding.PEM)
        cert_path.write_bytes(chain)
        self._write_key(key_path, key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return context

    @staticmethod
    def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
        """Write a private key that only the owner can read, from the first byte on."""
        data = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

        # The mode of `os.open` only counts for a new file, so an old one gets it here.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)

    @staticmethod
    def _alt_name(host: str) -> x509.GeneralName:
        """Name the host as an IP address when it is one, since clients check IPs against that field.

        Returns:
            The subject alternative name.
        """
        try:
            return x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            return x509.DNSName(host)
