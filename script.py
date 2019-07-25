#! /usr/bin/env python3
"""Example script for UDPack. Requires Python 3.7 or above, but should be
easily adaptable down to Python 3.4.

Creates a pair of packer / unpacker that emulated OpenVPN's "XOR patch".
"""
import asyncio
import functools
import logging

import udpack


async def run_packers():
    loop = asyncio.get_running_loop()
    # Create packer pipeline, emulating the XOR patch
    pipeline = [udpack.XorPtrPosPacker,
                udpack.ReverseOnePlusPacker,
                udpack.XorPtrPosPacker,
                functools.partial(udpack.XorMaskPacker, b'hello world!')]
    # Create client-side packer
    await udpack.create_udpack(
        loop,
        functools.partial(udpack.PipelineManager, pipeline, False),
        ('127.0.0.1', 7000),
        ('127.0.0.1', 7050),
        10, 20)
    # Create server-side unpacker
    await udpack.create_udpack(
        loop,
        functools.partial(udpack.PipelineManager, pipeline, True),
        ('127.0.0.1', 7050),
        ('127.0.0.1', 7100),
        10, 20)
    # Wait forever
    while True:
        await asyncio.sleep(1)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(run_packers())
