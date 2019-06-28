# UDPack
UDPack is an extensible generic UDP packet obfuscator. The purpose of this application is to sit in the path of a UDP data stream, and obfuscate, deobfuscate or otherwise modify the packets.

Python 3.4 or above is required, since this script uses the `asyncio` library. No hard 3rd-party dependencies. Optional dependency: `PyNaCl`.

**Warning:** It must be stressed that the purpose of this application is *obfuscation*, not *encryption*. Many design decisions have been (and will be) deliberately made against best practices in cryptography, so in all likelihood the obfuscation methods will not resist crypto analysis. **DO NOT** rely on the obfuscation for confidentiality of your data!!!

**This is a complete rewrite. The old single-file version lives on in the `old_monolithic` branch.** The old approach of putting obfuscation and network handling all in the same class is ... messy, to put it kindly, and all the subclasses and mixins doesn't make it any better. This new version separates the network part into a separate Manager class and keeps obfuscation in the packers, and use class factory functions instead of mixins to produce derived packers. This makes it much easier to chain different packers together, as demonstrated in the new `PipelineManager`.

These packers are available:

* `NoOpPacker`, `CallSoonPacker`: doesn't actually obfuscate anything.

* `ConstDelayPacker`, `DelayPacker`: delays each datagram for a fixed / variable amount of time.

* `RandomDropPacker`: drops packets randomly.

* `ShufflePacker`: shuffles the order of data bytes deterministically, using the random seed as key.

* `XorMaskPacker`, `ReverseOnePlusPacker`, `XorPtrPosPacker`: inspired by the "XOR Patch" for OpenVPN, these 3 packers when combined can emulate any obfuscation method in that patch.

* **New**: `XChaCha20Poly1305Packer`: Encrypt each datagram using XChaCha20Poly1305, making the contents indistinguishable from random bytes. 40 bytes overhead for each datagram. Available if `PyNaCl` is installed.

## Typical usage

A "packer" is a particular implementation of obfuscation method, obfuscating packets travelling upstream and deobfuscating packets travelling downstream. An "unpacker" is the same thing, implemented in the opposite direction.

Typically, an unpacker is set up near the server, with its `remote_addr` pointing to the listening `address:port` of the server. One or more packer is set up near the client(s), pointing at the listening `addr:port` of the unpacker. All packers and unpackers should have matching configurations. Finally, clients connect to the packer's listening `addr:port`. The packer obfuscates any traffic it receives and sends them to the unpacker, which deobfuscate and forward them to the server.


                raw data         obfuscated data           raw data
    UDP Client ---------- Packer =============== Unpacker ---------- UDP server
                 upstream ==>                      <== downstream

## Notes to developers

The configuration file is no more. Just construct your packer pipeline in code!