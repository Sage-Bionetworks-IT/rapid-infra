import hashlib
import io
import os

import aws_encryption_sdk
import pytest
from aws_encryption_sdk.identifiers import CommitmentPolicy, EncryptionKeyType, WrappingAlgorithm
from aws_encryption_sdk.internal.crypto.wrapping_keys import WrappingKey
from aws_encryption_sdk.key_providers.raw import RawMasterKeyProvider
from botocore.exceptions import ClientError

import encrypt

CSE = {
    "bucket": "rapid-test-a-encrypted",
    "patterns": ["**/*.vcf", "clinical/**"],
    "key_arn": "arn:aws:kms:us-east-1:111111111111:alias/cse/rapid-test-a-encrypted",
    "provider": "a",
}
ITEM = {
    "bucket": "rapid-test-a",
    "project_id": "syn111",
    "storage_location_id": 42,
    "encrypted_storage_location_id": 43,
    "cse": CSE,
}


class StaticKeys(RawMasterKeyProvider):
    """A raw AES wrapping key in place of KMS."""

    provider_id = "test"

    def __init__(self, **kwargs):
        self._key = os.urandom(32)

    def _get_raw_key(self, key_id):
        return WrappingKey(
            wrapping_algorithm=WrappingAlgorithm.AES_256_GCM_IV12_TAG16_NO_PADDING,
            wrapping_key=self._key,
            wrapping_key_type=EncryptionKeyType.SYMMETRIC,
        )


def _error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeS3:
    def __init__(self):
        self.objects = {}  # (bucket, key) -> (body, etag, metadata)
        self.deleted = []
        self.get_calls = []

    def put(self, bucket, key, body, etag='"e1"', metadata=None):
        self.objects[(bucket, key)] = (body, etag, metadata or {})

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _error("404")
        body, etag, metadata = self.objects[(Bucket, Key)]
        return {"ETag": etag, "ContentLength": len(body), "Metadata": metadata}

    def get_object(self, Bucket, Key, IfMatch=None):
        self.get_calls.append((Bucket, Key))
        body, etag, _ = self.objects[(Bucket, Key)]
        if IfMatch is not None and IfMatch != etag:
            raise _error("PreconditionFailed")
        return {"Body": io.BytesIO(body), "ETag": etag}

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None, Config=None):
        data = b""
        while chunk := fileobj.read(7919):
            data += chunk
        self.put(bucket, key, data, etag='"multi-3"', metadata=ExtraArgs["Metadata"])
        self.extra_args = ExtraArgs

    def delete_object(self, Bucket, Key, IfMatch=None):
        if (Bucket, Key) not in self.objects:
            raise _error("NoSuchKey")
        if IfMatch != self.objects[(Bucket, Key)][1]:
            raise _error("PreconditionFailed")
        del self.objects[(Bucket, Key)]
        self.deleted.append((Bucket, Key))


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(encrypt, "s3", fake)
    monkeypatch.setenv("SIDECAR_FILENAME", "sidecar.json")
    return fake


@pytest.fixture
def keys(monkeypatch):
    provider = StaticKeys()
    provider.add_master_key("k1")
    monkeypatch.setattr(encrypt, "_key_provider", lambda arn: provider)
    return provider


def _decrypt(ciphertext, keys):
    client = aws_encryption_sdk.EncryptionSDKClient(
        commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT
    )
    return client.decrypt(source=ciphertext, key_provider=keys)


@pytest.mark.parametrize(
    "key, expected",
    [
        ("batch1/sample.vcf", True),
        ("batch1/deep/er/sample.vcf", True),
        ("clinical/batch1/notes.csv", True),
        ("batch1/sample.vcf.gz", False),
        ("batch1/clinical/notes.csv", False),
        ("batch1/readme.txt", False),
    ],
)
def test_matches(key, expected):
    assert encrypt.matches(key, CSE["patterns"]) is expected


def test_matching_file_is_encrypted_round_trip(s3, keys):
    plaintext = os.urandom(300_000)
    s3.put("rapid-test-a", "batch1/sample.vcf", plaintext, etag='"abc"')

    item = encrypt.handler({**ITEM, "key": "batch1/sample.vcf"}, None)

    body, _, metadata = s3.objects[("rapid-test-a-encrypted", "batch1/sample.vcf.encrypted")]
    assert item == {
        "bucket": "rapid-test-a-encrypted",
        "key": "batch1/sample.vcf.encrypted",
        "project_id": "syn111",
        "storage_location_id": 43,
        "content_md5": hashlib.md5(body).hexdigest(),
        "staged": {"bucket": "rapid-test-a", "key": "batch1/sample.vcf", "etag": '"abc"'},
    }
    assert metadata == {"source-etag": '"abc"', "source-size": "300000"}
    assert s3.extra_args["ContentType"] == "application/octet-stream"
    assert plaintext not in body
    decrypted, header = _decrypt(body, keys)
    assert decrypted == plaintext
    assert header.encryption_context["rapid:provider"] == "a"
    assert header.encryption_context["rapid:bucket"] == "rapid-test-a"
    assert header.encryption_context["rapid:key"] == "batch1/sample.vcf"
    # Nothing is deleted before Synapse has indexed the ciphertext
    assert ("rapid-test-a", "batch1/sample.vcf") in s3.objects


@pytest.mark.parametrize("key", ["batch1/readme.txt", "batch1/sidecar.json", "batch1/"])
def test_unmatched_file_passes_through(s3, keys, key):
    item = encrypt.handler({**ITEM, "key": key}, None)
    assert item == {
        "bucket": "rapid-test-a",
        "key": key,
        "project_id": "syn111",
        "storage_location_id": 42,
    }
    assert not s3.get_calls


def test_already_encrypted_file_is_not_re_encrypted(s3, keys):
    s3.put("rapid-test-a", "batch1/sample.vcf", b"data", etag='"abc"')
    s3.put(
        "rapid-test-a-encrypted",
        "batch1/sample.vcf.encrypted",
        b"ciphertext",
        metadata={"source-etag": '"abc"'},
    )
    item = encrypt.handler({**ITEM, "key": "batch1/sample.vcf"}, None)
    assert "content_md5" not in item  # Sync hashes the existing ciphertext
    assert item["staged"]["etag"] == '"abc"'
    assert not s3.get_calls
    assert s3.objects[("rapid-test-a-encrypted", "batch1/sample.vcf.encrypted")][0] == b"ciphertext"


def test_changed_file_is_re_encrypted(s3, keys):
    s3.put("rapid-test-a", "batch1/sample.vcf", b"new data", etag='"new"')
    s3.put(
        "rapid-test-a-encrypted",
        "batch1/sample.vcf.encrypted",
        b"old ciphertext",
        metadata={"source-etag": '"old"'},
    )
    item = encrypt.handler({**ITEM, "key": "batch1/sample.vcf"}, None)
    body = s3.objects[("rapid-test-a-encrypted", "batch1/sample.vcf.encrypted")][0]
    assert _decrypt(body, keys)[0] == b"new data"
    assert item["content_md5"] == hashlib.md5(body).hexdigest()


def test_file_over_the_limit_fails(s3, keys, monkeypatch):
    monkeypatch.setenv("MAX_BYTES", "10")
    s3.put("rapid-test-a", "batch1/sample.vcf", b"x" * 11)
    with pytest.raises(encrypt.FileTooLargeError, match="11 bytes"):
        encrypt.handler({**ITEM, "key": "batch1/sample.vcf"}, None)
    assert not s3.get_calls


def test_delete_staged_deletes_the_encrypted_plaintext(s3):
    s3.put("rapid-test-a", "batch1/sample.vcf", b"data", etag='"abc"')
    event = {
        "key": "batch1/sample.vcf.encrypted",
        "status": "created",
        "staged": {"bucket": "rapid-test-a", "key": "batch1/sample.vcf", "etag": '"abc"'},
    }
    assert encrypt.delete_staged(event, None) == event
    assert s3.deleted == [("rapid-test-a", "batch1/sample.vcf")]


@pytest.mark.parametrize("present", [True, False])
def test_delete_staged_keeps_a_replaced_or_missing_file(s3, present):
    if present:
        # Re-uploaded since it was encrypted: its own sidecar will encrypt it
        s3.put("rapid-test-a", "batch1/sample.vcf", b"newer", etag='"newer"')
    staged = {"bucket": "rapid-test-a", "key": "batch1/sample.vcf", "etag": '"abc"'}
    encrypt.delete_staged({"staged": staged}, None)
    assert not s3.deleted
    assert (("rapid-test-a", "batch1/sample.vcf") in s3.objects) is present


def test_delete_staged_raises_other_errors(s3, monkeypatch):
    def denied(**kwargs):
        raise _error("AccessDenied")

    monkeypatch.setattr(s3, "delete_object", denied)
    staged = {"bucket": "rapid-test-a", "key": "batch1/sample.vcf", "etag": '"abc"'}
    with pytest.raises(ClientError):
        encrypt.delete_staged({"staged": staged}, None)
