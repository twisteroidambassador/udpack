# UDPack
UDPack is an extensible generic UDP packet obfuscator. The purpose of this application is to sit in the path of a UDP data stream, and obfuscate, deobfuscate or otherwise modify the packets.

Python 3.4 or above is required, since this script uses the `asyncio` library. Currently there are no external dependencies.

**Warning:** It must be stressed that the purpose of this application is *obfuscation*, not *encryption*. Many design decisions have been (and will be) deliberately made against best practices in cryptography, so in all likelihood the obfuscation methods will not resist crypto analysis. **DO NOT** rely on the obfuscation for confidentiality of your data!!!

At this stage the script includes the following "packers" (obfuscation methods):

* Straight through: do nothing, just forward the packet along

* Shuffle: shuffle the order of data bytes in each packet

* XORPatch: emulate the famous "XOR patch" for OpenVPN. This should be helpful in these scenarios: vanilla OpenVPN client ==> XORPatchPacker ==> XOR-patched OpenVPN server, and XOR-Patched OpenVPN client ==> XORPatchUnpacker ==> vanilla OpenVPN server. (Note that this packer has not been actually tested to work in these scenarios. [Please report your results here: Issue 1](https://github.com/twisteroidambassador/udpack/issues/1).)

* Toy Model Encryption: implements a toy model padding + encryption + authentication scheme, using the Mersenne Twister PRNG as a stream cipher and truncated SHA-1 HMAC for authentication. Each packet can be padded to a random length, and every byte in the packet is randomized so there is no obvious packet signature. The protocol has a minimal overhead of 8 bytes. As the name implies, the cryptographic strength of this "encryption" scheme is quite low.

  There is also a "delay" version, which randomly delays each obfuscated packet to help thwart inter-arrival time analysis. (Packets may be sent out of order.)

## Typical usage

A "packer" is a particular implementation of obfuscation method, obfuscating packets travelling upstream and deobfuscating packets travelling downstream. An "unpacker" is the same thing, implemented in the opposite direction.

Typically, an unpacker is set up near the server, with its `remote_addr` pointing to the listening `address:port` of the server. One or more packer is set up near the client(s), pointing at the listening `addr:port` of the unpacker. All packers and unpackers should have matching configurations. Finally, clients connect to the packer's listening `addr:port`. The packer obfuscates any traffic it receives and sends them to the unpacker, which deobfuscate and forward them to the server.


                raw data         obfuscated data           raw data
    UDP Client ---------- Packer =============== Unpacker ---------- UDP server
                 upstream ==>                      <== downstream

## Notes to developers

The configuration sections are somewhat bloated in order to support all the different packers implemented. If your use case does not need this much flexibility, consider removing unused packers and rewriting `main_cli()` to only keep relevant parts. 

Configuration options are currently passed into packers via a `configparser` object, where all options are strings, but the packers themselves are written to also accept an ordinary dictionary with appropriate data types.

To implement a new packer, simply inherit from `UDPackStraightThroughPacker` and override methods you need. Often it's only necessary to implement `pack` and `unpack`. To implement the corresponding unpacker, inherit from `UDPackUnpackerMixIn` and the packer.

Inherit from `UDPackRandomDelayMixIn` to quickly add a random delay feature to your packer.
