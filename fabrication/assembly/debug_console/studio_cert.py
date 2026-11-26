"""Create the local certificates that let the PICO browser open Studio over HTTPS.

Writes a small private CA and a Studio server certificate for the robot's
addresses. Private keys stay in the given folder (default ``.local-deploy``,
ignored by Git); only ``ca.crt`` is meant to be copied to the headset.
An existing CA is reused so certificates already installed on the headset keep
working when the server certificate is renewed.

    python -m fabrication.assembly.debug_console.studio_cert --ip 192.168.137.150
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_DIR = REPO / '.local-deploy' / 'studio-tls'


def _modules():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:
        raise SystemExit('需要 cryptography 包；可在工作端生成后把 studio-tls 文件夹复制到车载') from exc
    return x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def create(folder=DEFAULT_DIR, addresses=('192.168.137.150',), names=('localhost',), days=825, now=None):
    x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID = _modules()
    folder = Path(folder)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pem = serialization.Encoding.PEM
    plain = serialization.NoEncryption()
    ca_key_path, ca_path = folder / 'ca.key', folder / 'ca.crt'
    if ca_key_path.exists() and ca_path.exists():
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), None)
        ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'XLeRobot Studio Local CA')])
        ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                           content_commitment=False, key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        _write(ca_key_path, ca_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, plain))
        _write(ca_path, ca.public_bytes(pem))
    ips = sorted({ipaddress.ip_address(a) for a in (*addresses, '127.0.0.1')}, key=str)
    key = ec.generate_private_key(ec.SECP256R1())
    alt = x509.SubjectAlternativeName([*(x509.DNSName(n) for n in names), *(x509.IPAddress(i) for i in ips)])
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'XLeRobot Studio')]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(alt, critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write(folder / 'server.key', key.private_bytes(pem, serialization.PrivateFormat.PKCS8, plain))
    _write(folder / 'server.crt', cert.public_bytes(pem) + ca.public_bytes(pem))
    return {'folder': str(folder), 'addresses': [str(i) for i in ips], 'names': list(names),
            'ca_sha256': ca.fingerprint(hashes.SHA256()).hex(':').upper(),
            'expires': getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create Studio HTTPS certificates for the PICO browser')
    parser.add_argument('--ip', action='append', default=[], help='robot address, repeatable')
    parser.add_argument('--name', action='append', default=[], help='extra DNS name, repeatable')
    parser.add_argument('--out', default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)
    result = create(args.out, args.ip or ['192.168.137.150'], ['localhost', *args.name])
    for key, value in result.items():
        print(f'{key}: {value}')


if __name__ == '__main__':
    main()
