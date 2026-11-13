# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit test of the throwaway Cloud KMS stand-in of smoketest_relay_signer. The real relay signer daemon is not present on the dev
box, so a stub `main` module with the one function the stand-in uses (`cloudkms_shim.crc32c`) is put in its place."""

import sys
import types
import unittest
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


class ThrowawayKmsTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("main")
        sys.modules["main"] = types.SimpleNamespace(cloudkms_shim=types.SimpleNamespace(crc32c=zlib.crc32))
        sys.modules.pop("smoketest_relay_signer", None)
        import smoketest_relay_signer
        self.module = smoketest_relay_signer

    def tearDown(self):
        sys.modules.pop("smoketest_relay_signer", None)
        if self._saved is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = self._saved

    def test_public_key_and_signature_belong_together(self):
        kms = self.module.ThrowawayKms()
        public_pem = kms.get_public_key({"name": "k"}).pem
        public_key = serialization.load_pem_public_key(public_pem.encode())
        digest = hashes.Hash(hashes.SHA256())
        digest.update(b"message")
        value = digest.finalize()
        answer = kms.asymmetric_sign({"name": "k", "digest": {"sha256": value}})
        public_key.verify(answer.signature, value, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        self.assertEqual(answer.name, "k")

    def test_every_instance_has_its_own_random_key(self):
        first, second = self.module.ThrowawayKms(), self.module.ThrowawayKms()
        self.assertNotEqual(first.get_public_key({}).pem, second.get_public_key({}).pem)


# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: smoketest_relay_signer:smoketest_relay_signer_throwaway_kms_class]
