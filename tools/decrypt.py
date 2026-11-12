"""
Decrypt a client-side encrypted RAPID file (a *.encrypted file indexed in Synapse):

    python decrypt.py --key-arn <CseKeyArn> syn123            # download from Synapse, decrypt
    python decrypt.py --key-arn <CseKeyArn> ./data.vcf.encrypted

1. With a Synapse ID, downloads the file (Synapse checks your access as for any download)
2. Decrypts it with the AWS Encryption SDK under the provider's CSE KMS key: your AWS
   credentials must be one of the bucket's `decrypt_arns` (KMS checks that). The key is named
   by its ARN (the bucket stack's CseKeyArn output); the file must have been encrypted under
   exactly that key, and for a RAPID provider (its encryption context)
3. Writes the plaintext next to it, without the .encrypted suffix (or to --output). The file
   is written as it is decrypted and only renamed into place once its signature checks out

Needs `pip install aws-encryption-sdk boto3` (and synapseclient for a Synapse ID). Logs in to
Synapse with SYNAPSE_AUTH_TOKEN if set, else synapseclient's default (~/.synapseConfig).

The same, with the AWS Encryption CLI (pip install aws-encryption-sdk-cli):

    aws-encryption-cli --decrypt --input data.vcf.encrypted --output data.vcf \\
        --wrapping-keys key=<CseKeyArn> --commitment-policy require-encrypt-require-decrypt \\
        --encryption-context rapid:provider=<bucket id> --buffer
"""

import argparse
import os
import re
import sys
from pathlib import Path

SUFFIX = ".encrypted"
PROVIDER_CONTEXT = "rapid:provider"
_SYNAPSE_ID = re.compile(r"^syn\d+(\.\d+)?$")
BLOCK_SIZE = 8 * 1024**2


def output_path(source: Path, output: Path | None) -> Path:
    if output is not None:
        return output
    if source.name.endswith(SUFFIX) and len(source.name) > len(SUFFIX):
        return source.with_name(source.name[: -len(SUFFIX)])
    return source.with_name(source.name + ".decrypted")


def decrypt_file(source: Path, target: Path, key_provider, provider: str | None = None) -> dict:
    """
    Stream-decrypt source into target. Returns the message's encryption context. Raises (and
    leaves no target) when the key, the signature or the context does not match.
    """
    import aws_encryption_sdk
    from aws_encryption_sdk.identifiers import CommitmentPolicy

    client = aws_encryption_sdk.EncryptionSDKClient(
        commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT
    )
    partial = target.with_name(target.name + ".partial")
    try:
        with open(source, "rb") as src, open(partial, "wb") as dst:
            with client.stream(mode="d", source=src, key_provider=key_provider) as decryptor:
                context = decryptor.header.encryption_context
                if PROVIDER_CONTEXT not in context:
                    raise ValueError(f"{source} was not encrypted by the RAPID pipeline")
                if provider is not None and context[PROVIDER_CONTEXT] != provider:
                    raise ValueError(
                        f"{source} belongs to provider '{context[PROVIDER_CONTEXT]}', "
                        f"not '{provider}'"
                    )
                while chunk := decryptor.read(BLOCK_SIZE):
                    dst.write(chunk)
        # Only now has the signature at the end of the message been verified
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return dict(context)


def _download(synapse_id: str, directory: Path) -> Path:
    import synapseclient
    from synapseclient.models import File

    syn = synapseclient.Synapse(skip_checks=True, silent=True)
    token = os.environ.get("SYNAPSE_AUTH_TOKEN")
    syn.login(authToken=token, silent=True) if token else syn.login(silent=True)
    entity_id, _, version = synapse_id.partition(".")
    file = File(
        id=entity_id,
        version_number=int(version) if version else None,
        path=str(directory),
        if_collision="overwrite.local",
    ).get(synapse_client=syn)
    return Path(file.path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="A Synapse ID (syn123, syn123.2) or a local .encrypted file")
    parser.add_argument("--key-arn", required=True, help="The CSE key's ARN (CseKeyArn output)")
    parser.add_argument("--provider", help="Also require this provider (the bucket's id)")
    parser.add_argument("--output", type=Path, help="Plaintext path (default: drop .encrypted)")
    parser.add_argument(
        "--download-dir", type=Path, default=Path("."), help="Where a Synapse file is downloaded"
    )
    args = parser.parse_args(argv)
    if not args.key_arn.startswith("arn:") or ":key/" not in args.key_arn:
        sys.exit("--key-arn must be the key's ARN (arn:aws:kms:...:key/...), not an alias")

    import aws_encryption_sdk

    source = (
        _download(args.source, args.download_dir)
        if _SYNAPSE_ID.match(args.source)
        else Path(args.source)
    )
    target = output_path(source, args.output)
    key_provider = aws_encryption_sdk.StrictAwsKmsMasterKeyProvider(key_ids=[args.key_arn])
    try:
        context = decrypt_file(source, target, key_provider, args.provider)
    except Exception as e:  # KMS AccessDenied, a wrong key, a tampered file
        sys.exit(f"Could not decrypt {source}: {e}")
    print(
        f"Decrypted {source} → {target} "
        f"(provider {context[PROVIDER_CONTEXT]}, s3://{context.get('rapid:bucket')}/"
        f"{context.get('rapid:key')})"
    )


if __name__ == "__main__":
    main()
