import io
import os
import sys
from pathlib import Path

import pytest

import encrypt
from .test_encrypt import CSE, StaticKeys

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
import decrypt  # noqa: E402


@pytest.fixture
def keys():
    provider = StaticKeys()
    provider.add_master_key("k1")
    return provider


class _Sink:
    """Just enough S3 for encrypt_object: the source body in, the ciphertext out."""

    def __init__(self, plaintext):
        self.plaintext, self.ciphertext = plaintext, None

    def get_object(self, Bucket, Key, IfMatch=None):
        return {"Body": io.BytesIO(self.plaintext)}

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None, Config=None):
        self.ciphertext = b"".join(iter(lambda: fileobj.read(65536), b""))


def _encrypted_file(tmp_path, keys, monkeypatch, plaintext):
    monkeypatch.setattr(encrypt, "_key_provider", lambda arn: keys)
    sink = _Sink(plaintext)
    encrypt.encrypt_object(sink, CSE, "rapid-test-a", "b/data.vcf", '"e"', len(plaintext))
    path = tmp_path / "data.vcf.encrypted"
    path.write_bytes(sink.ciphertext)
    return path


def test_decrypts_what_the_pipeline_encrypted(tmp_path, keys, monkeypatch):
    plaintext = os.urandom(20 * 1024**2 + 3)  # several read blocks
    source = _encrypted_file(tmp_path, keys, monkeypatch, plaintext)
    target = decrypt.output_path(source, None)
    assert target == tmp_path / "data.vcf"
    context = decrypt.decrypt_file(source, target, keys, provider="a")
    assert target.read_bytes() == plaintext
    assert context["rapid:key"] == "b/data.vcf"
    assert not list(tmp_path.glob("*.partial"))


def test_wrong_provider_leaves_no_plaintext(tmp_path, keys, monkeypatch):
    source = _encrypted_file(tmp_path, keys, monkeypatch, b"secret")
    target = tmp_path / "data.vcf"
    with pytest.raises(ValueError, match="provider 'a'"):
        decrypt.decrypt_file(source, target, keys, provider="b")
    assert not target.exists() and not list(tmp_path.glob("*.partial"))


def test_tampered_file_leaves_no_plaintext(tmp_path, keys, monkeypatch):
    source = _encrypted_file(tmp_path, keys, monkeypatch, os.urandom(100_000))
    data = bytearray(source.read_bytes())
    data[-200] ^= 1  # in the last frame / signature
    source.write_bytes(bytes(data))
    target = tmp_path / "data.vcf"
    with pytest.raises(Exception):
        decrypt.decrypt_file(source, target, keys)
    assert not target.exists() and not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize(
    "name, expected", [("a.vcf.encrypted", "a.vcf"), ("a.vcf", "a.vcf.decrypted")]
)
def test_output_path(tmp_path, name, expected):
    assert decrypt.output_path(tmp_path / name, None) == tmp_path / expected


def test_key_must_be_an_arn():
    with pytest.raises(SystemExit, match="not an alias"):
        decrypt.main(["--key-arn", "alias/cse/x", "f.encrypted"])
