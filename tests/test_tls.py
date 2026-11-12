import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
from cryptography import x509

from lesstests.tls import CertificateAuthority


def test_the_ca_is_made_once_and_then_reused(tmp_path: Path) -> None:
    first = CertificateAuthority.load_or_create(tmp_path / "ca")
    pem = first.ca_pem.read_bytes()
    second = CertificateAuthority.load_or_create(tmp_path / "ca")
    assert second.ca_pem.read_bytes() == pem
    assert x509.load_pem_x509_certificate(pem).extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_every_private_key_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    authority = CertificateAuthority.load_or_create(tmp_path / "ca")
    authority.server_context("www.googleapis.com")
    keys = sorted((tmp_path / "ca").rglob("*key.pem"))
    assert len(keys) == 2
    assert {key.stat().st_mode & 0o777 for key in keys} == {0o600}


def test_a_host_certificate_names_the_host_and_is_cached(tmp_path: Path) -> None:
    authority = CertificateAuthority.load_or_create(tmp_path)
    context = authority.server_context("www.googleapis.com")
    assert authority.server_context("www.googleapis.com") is context
    chain = (tmp_path / "hosts" / "www.googleapis.com-cert.pem").read_bytes()
    leaf = x509.load_pem_x509_certificates(chain)[0]
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["www.googleapis.com"]
    assert isinstance(context, ssl.SSLContext)


def test_an_ip_host_gets_an_ip_name(tmp_path: Path) -> None:
    authority = CertificateAuthority.load_or_create(tmp_path)
    authority.server_context("127.0.0.1")
    leaf = x509.load_pem_x509_certificates((tmp_path / "hosts" / "127.0.0.1-cert.pem").read_bytes())[0]
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in names.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


@pytest.mark.skipif(shutil.which("keytool") is None, reason="needs keytool from a JDK")
def test_java_truststore_holds_the_ca_and_is_not_rewritten(tmp_path: Path) -> None:
    authority = CertificateAuthority.load_or_create(tmp_path / "ca")
    store = authority.java_truststore(tmp_path / "trust.p12", "secret")
    listing = subprocess.run(
        ["keytool", "-list", "-keystore", str(store), "-storetype", "PKCS12", "-storepass", "secret"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "lesstests-ca" in listing.stdout
    written = store.stat().st_mtime_ns
    authority.java_truststore(store, "secret")
    assert store.stat().st_mtime_ns == written
