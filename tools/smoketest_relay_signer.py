#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
# This software is Proprietary, Trade-Secret.
"""The relay signer for a `provision.py --test` host: the real daemons/relay_signer, with a throwaway key in place of Cloud KMS.

hams-relay-signer.service signs with a Google Cloud KMS HSM key, which a test host neither can nor should reach. The callbook
delta exporter and the relay satellite and manuals publishers send what they publish to that signer, so a test host runs this
instead (provision.py installs a drop-in for the unit, in test mode only). It loads the daemon's own code
(/opt/hams/daemons/relay_signer/main.py), serves the same socket, and signs with a fresh random P-256 key held in this process:
the key is never written anywhere and is not the production issuer key, so nothing it signs is trusted by any relay.
"""

import hashlib
import os
import sys
import types

sys.path[:0] = ["/opt/hams/daemons/relay_signer", "/opt/hams/daemons"]
import main as signer  # noqa: E402  (daemons/relay_signer/main.py; it appends daemons/relay_ca for cloudkms_shim)
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

KEY_VERSION = "projects/test/locations/none/keyRings/test/cryptoKeys/throwaway/cryptoKeyVersions/1"


class ThrowawayKms:
    """The two Cloud KMS calls relay_signer makes, answered by a random software key made at start."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def get_public_key(self, request, timeout=None):
        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return types.SimpleNamespace(pem=pem, pem_crc32c=signer.cloudkms_shim.crc32c(pem.encode()))

    def asymmetric_sign(self, request, timeout=None):
        (_field, digest), = request["digest"].items()
        signature = self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return types.SimpleNamespace(
            name=request["name"], signature=signature,
            signature_crc32c=signer.cloudkms_shim.crc32c(signature), verified_digest_crc32c=True)


def main():
    kms = ThrowawayKms()
    der = kms.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    env = dict(os.environ)
    env["RELAY_CA_KMS_KEY_VERSION"] = KEY_VERSION
    env["RELAY_CA_PINNED_SPKI_SHA256"] = hashlib.sha256(der).hexdigest()
    return signer.main(env=env, argv=[sys.argv[0]], kms_client=kms)


if __name__ == "__main__":
    sys.exit(main())
