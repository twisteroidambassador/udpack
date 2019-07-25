"""Implements plug-in packers and metaclasses used to modify packers."""

import asyncio
import itertools
import random

__all__ = ['make_pack_only_packer', 'make_unpack_only_packer', 'make_reverse_packer',
           'NoOpPacker', 'CallSoonPacker', 'ConstDelayPacker', 'DelayPacker',
           'RandomDropPacker', 'ShufflePacker', 'XorMaskPacker',
           'ReverseOnePlusPacker', 'XorPtrPosPacker']


class BasePacker():
    """Base class for all packers.
    
    Methods:
    pack(data), unpack(data): self-explanatory.
    call_packed_cb(data): call the current set packed_cb.
    call_unpacked_cb(data): call the current set unpacked_cb.
    
    Attributes:
    packed_cb: a callback to be called for every packed datagram.
    unpacked_cb: a callback to be called for every unpacked datagram.
    """

    def __init__(self, *, packed_cb=None, unpacked_cb=None):
        """Initialize Packer.
        
        Arguments:
        packed_cb, unpacked_cb: same as the attributes.
        
        Both the above arguments are optional, but they must be set before
        calling pack() or unpack().
        """
        self.packed_cb = packed_cb
        self.unpacked_cb = unpacked_cb

    def pack(self, data):
        raise NotImplementedError

    def call_packed_cb(self, data):
        """Call packed_cb() of this packer instance.
        
        Use this method if you need to be able to call the packed_cb() in
        effect at any time. For instance:
        
        packer = Packer(packed_cb=old_callback)
        callback_handle_1 = packer.packed_cb
        callback_handle_2 = packer.call_packed_cb
        packer.packed_cb = new_callback
        callback_handle_1(data) # This calls old_callback
        callback_handle_2(data) # This calls new_callback
        """
        self.packed_cb(data)

    def unpack(self, data):
        raise NotImplementedError

    def call_unpacked_cb(self, data):
        """Call unpacked_cb() of this packer instance.
        
        See call_packed_cb for why you may need this.
        """
        self.unpacked_cb(data)


class NoOpPacker(BasePacker):
    """Forward packets immediately as-is."""

    def pack(self, data):
        self.packed_cb(data)

    def unpack(self, data):
        self.unpacked_cb(data)


def make_pack_only_packer(packer_class):
    """Return a new Packer class that only does packing (unpacking is NoOp).
    
    The unpack() method in the new class will be the same as in NoOpPacker."""
    return type('PackOnly' + packer_class.__name__, (packer_class,),
                {'unpack': NoOpPacker.unpack})


def make_unpack_only_packer(packer_class):
    """Return a new Packer class that only does unpacking (packing is NoOp).
    
    The pack() method in the new class will be the same as in NoOpPacker."""
    return type('UnpackOnly' + packer_class.__name__, (packer_class,),
                {'pack': NoOpPacker.pack})


def make_reverse_packer(packer_class):
    """Return a new Packer class in the opposite direction.
    
    The pack() method in the new class is the unpack() in the old class, and
    vice versa.
    """

    class ReversePacker(BasePacker):
        def __init__(self, *args, packed_cb=None, unpacked_cb=None, **kwargs):
            super().__init__(packed_cb=packed_cb, unpacked_cb=unpacked_cb)
            self._inner_packer = packer_class(
                *args, packed_cb=self.call_unpacked_cb,
                unpacked_cb=self.call_packed_cb, **kwargs)

        def pack(self, data):
            self._inner_packer.unpack(data)

        def unpack(self, data):
            self._inner_packer.pack(data)

    ReversePacker.__name__ = 'Reverse' + packer_class.__name__
    return ReversePacker


class CallSoonPacker(BasePacker):
    """Forward packets using loop.call_soon().
    
    This should give the event loop a chance to run between when the datagram
    is received and when the datagram is forwarded.
    """

    def __init__(self, *args, loop=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._loop = loop or asyncio.get_event_loop()

    def pack(self, data):
        self._loop.call_soon(self.packed_cb, data)

    def unpack(self, data):
        self._loop.call_soon(self.unpacked_cb, data)


class ConstDelayPacker(BasePacker):
    """Delay packets for a set amount of time.
    
    See BasePacker for methods and attributes.
    
    Additional attributes:
    pack_delay, unpack_delay: number of seconds to delay each datagram in that 
        direction.
    """

    def __init__(self, pack_delay, unpack_delay, *args, loop=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._loop = loop or asyncio.get_event_loop()
        self.pack_delay = pack_delay
        self.unpack_delay = unpack_delay

    def pack(self, data):
        self._loop.call_later(self.pack_delay, self.call_packed_cb, data)

    def unpack(self, data):
        self._loop.call_later(self.unpack_delay, self.call_unpacked_cb, data)


class DelayPacker(NoOpPacker):
    """Delay packets according to result of function call.
    
    This is intended to be used with a function that returns a random number,
    so each packet can be delayed randomly.
    
    Additional attributes:
    pack_delay, unpack_delay: a function/callable that returns the number of
        seconds to delay. Called once for each datagram. Explicitly set to None
        to disable delay in that direction.
    """

    def __init__(self, pack_delay, unpack_delay, *args, loop=None, **kwargs):
        """Initialize Packer.
        
        Additional arguments:
        pack_delay, unpack_delay: same as attributes.
        """
        super().__init__(*args, **kwargs)
        self._loop = loop or asyncio.get_event_loop()
        self.pack_delay = pack_delay
        self.unpack_delay = unpack_delay

    @property
    def pack_delay(self):
        return self._pack_delay

    @pack_delay.setter
    def pack_delay(self, delay):
        self._pack_delay = delay
        if delay is None:
            self.pack = self._pack_no_delay
        else:
            self.pack = self._pack_with_delay

    @property
    def unpack_delay(self):
        return self._unpack_delay

    @unpack_delay.setter
    def unpack_delay(self, delay):
        self._unpack_delay = delay
        if delay is None:
            self.unpack = self._unpack_no_delay
        else:
            self.unpack = self._unpack_with_delay

    def _pack_no_delay(self, data):
        super().pack(data)

    def _unpack_no_delay(self, data):
        super().unpack(data)

    def _pack_with_delay(self, data):
        self._loop.call_later(self._pack_delay(), self.call_packed_cb, data)

    def _unpack_with_delay(self, data):
        self._loop.call_later(self._unpack_delay(), self.call_unpacked_cb, data)


class RandomDropPacker(BasePacker):
    """Randomly drop packets.
    
    Additional attributes:
    pack_drop_rate, unpack_drop_rate: number between 0 and 1 specifying ratio
        of datagrams to drop in that direction.
    """

    def __init__(self, pack_drop_rate, unpack_drop_rate, *args,
                 use_system_random=False, **kwargs):
        """Initialize Packer.
        
        Additional arguments:
        pack_drop_rate, unpack_drop_rate: same as attributes.
        use_system_random: whether to use random.SystemRandom as random source.
        """
        super().__init__(*args, **kwargs)
        self.pack_drop_rate = pack_drop_rate
        self.unpack_drop_rate = unpack_drop_rate
        if use_system_random:
            self._random = random.SystemRandom()
        else:
            self._random = random

    def pack(self, data):
        if self._random.random() >= self.pack_drop_rate:
            self.packed_cb(data)

    def unpack(self, data):
        if self._random.random() >= self.unpack_drop_rate:
            self.unpacked_cb(data)


class ShufflePacker(BasePacker):
    """Shuffle data byte order with a PRNG.
    
    The PRNG is seeded by the length of the packet + user selected key.
    """

    def __init__(self, key, *args, **kwargs):
        """Initialize Packer.
        
        Additinal arguments:
        key: integer that determines shuffle patterns. Both sides must have the
            same key.
        """
        super().__init__(*args, **kwargs)
        self._key = key
        self._random = random.Random()
        self._shuffle_sequence = {}

    def pack(self, data):
        shuffled = bytes(data[i] for i in self._get_shuffle_sequence(len(data))[0])
        self.packed_cb(shuffled)

    def unpack(self, data):
        unshuffled = bytes(data[i] for i in self._get_shuffle_sequence(len(data))[1])
        self.unpacked_cb(unshuffled)

    def _get_shuffle_sequence(self, length):
        if length not in self._shuffle_sequence:
            self._logger.debug('Generating shuffle sequence of length %d', length)
            self._random.seed(length + self.random_seed_key)
            s = list(range(length))
            self._random.shuffle(s)
            s2 = list(enumerate(s))
            s2.sort(key=lambda i: i[1])
            s3 = [i[0] for i in s2]
            self._shuffle_sequence[length] = (s, s3)
        return self.shuffle_sequence[length]


# The following 3 packers replicate the effect of OpenVPN's "XOR patch".
# https://tunnelblick.net/cOpenvpn_xorpatch.html
# To replicate the "obfuscate" option, use the following packer pipeline:
# [XorPtrPosPacker, ReverseOnePlusPacker, XorPtrPosPacker, XorMaskPacker]

class XorMaskPacker(BasePacker):
    """Byte-wise XOR the datagram with a mask (repeated when necessary)."""

    def __init__(self, mask, *args, **kwargs):
        """Initialize packer.
        
        Additional arguments:
        mask: some bytes to be XORed to data.
        """
        super().__init__(*args, **kwargs)
        self.mask = mask

    def _xor_with_mask(self, data):
        return bytes(a ^ b for a, b in zip(data, itertools.cycle(self.mask)))

    def pack(self, data):
        self.packed_cb(self._xor_with_mask(data))

    def unpack(self, data):
        self.unpacked_cb(self._xor_with_mask(data))


class ReverseOnePlusPacker(BasePacker):
    """Reverse order of bytes in the datagram except the first byte."""

    def _reverse_one_plus(self, data):
        return data[0:1] + data[:0:-1]

    def pack(self, data):
        self.packed_cb(self._reverse_one_plus(data))

    def unpack(self, data):
        self.unpacked_cb(self._reverse_one_plus(data))


class XorPtrPosPacker(BasePacker):
    """XOR each byte with its (1-based) position."""

    def _xor_ptr_pos(self, data):
        return bytes(((i + 1) & 255) ^ b for i, b in enumerate(data))

    def pack(self, data):
        self.packed_cb(self._xor_ptr_pos(data))

    def unpack(self, data):
        self.unpacked_cb(self._xor_ptr_pos(data))
