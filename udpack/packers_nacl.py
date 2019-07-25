try:
    import nacl.bindings.crypto_aead as aead
    from nacl.bindings.randombytes import randombytes

    import_exc = None
except ImportError as e:
    aead = None
    randomBytes = None
    import_exc = e

from . import packers_base

__all__ = ['XChaCha20Poly1305Packer']


class XChaCha20Poly1305Packer(packers_base.BasePacker):
    """Encrypt each datagram with XChaCha20Poly1305.

    XChaCha20Poly1305 is an AEAD construction that allows random nonces to
    be used safely. After encryption, the contents of each datagram should
    be indistinguishable from random bytes.

    Encrypted datagrams are 40 bytes longer than the plaintext (24 for the
    nonce, 16 for the authentication tag).
    """

    def __init__(self, key, *args, **kwargs):
        """Initialize packer.

        key must be 32 bytes long.
        """
        if aead is None:
            raise ImportError('Importing PyNaCl failed') from import_exc
        super().__init__(*args, **kwargs)
        self._key = key

    def pack(self, data):
        nonce = randombytes(aead.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
        ciphertext = aead.crypto_aead_xchacha20poly1305_ietf_encrypt(
            data, None, nonce, self._key)
        self.packed_cb(nonce + ciphertext)

    def unpack(self, data):
        if len(data) < aead.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES:
            raise ValueError('Message too short for nonce')
        nonce = data[:aead.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES]
        ciphertext = data[aead.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES:]
        plaintext = aead.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, None, nonce, self._key)
        self.unpacked_cb(plaintext)
