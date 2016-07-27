# UDPack
UDPack is an extensible generic UDP packet obfuscator. The purpose of this application is to sit in the path of a UDP data stream, and obfuscate, deobfuscate or otherwise modify the packets.

Python 3.4 or above is required, since this script uses the `asyncio` library. Currently there are no other dependencies.

**Warning:** It must be stressed that the purpose of this application is *obfuscation*, not *encryption*. Many design decisions have been (and will be) deliberately made against best practices in cryptography, so in all likelihood the obfuscation methods will not resist crypto analysis. **DO NOT** rely on the obfuscation for confidentiality of your data!!!

At this stage the script includes the following "packers" (obfuscation methods):

* Straight through: no obfuscation
* Shuffle: shuffle the order of data bytes in each packet

## Typical usage

A "packer" is a particular implementation of obfuscation method, usually obfuscating packets travelling upstream and deobfuscating packets travelling downstream. An "unpacker" is the same thing, implemented in the opposite direction.

                raw data         obfuscated data           raw data
    UDP Client ---------- Packer =============== Unpacker ---------- UDP server
                 upstream ==>                      <== downstream

## Implementing new packers and unpackers

In most cases, to implement a new packer, simply inherit from `UDPackStraightThroughPacker` and implement `pack` and `unpack` methods. To implement the corresponding unpacker, inherit from `UDPackUnpackerMixIn` and the completed packer.
