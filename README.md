# UDPack
UDPack is an extensible generic UDP packet obfuscator. The purpose of this application is to sit in the path of a UDP data stream, and obfuscate, deobfuscate or otherwise modify the packets.

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
