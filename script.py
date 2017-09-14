#! /usr/bin/env python3

import asyncio
import functools
import logging

import udpack

logging.basicConfig(level=logging.DEBUG)
logging.getLogger().setLevel(logging.DEBUG)

def main():
    
    loop = asyncio.get_event_loop()
    
    pipeline = [udpack.NoOpPacker, udpack.ReverseOnePlusPacker, udpack.ReverseOnePlusPacker]
    pack_pipeline = [udpack.XorPtrPosPacker, 
                     udpack.ReverseOnePlusPacker, 
                     udpack.XorPtrPosPacker, 
                     functools.partial(udpack.XorMaskPacker, b'hello world!')]
    unpack_pipeline = [functools.partial(udpack.XorMaskPacker, b'hello world!'),
                       udpack.XorPtrPosPacker, 
                       udpack.ReverseOnePlusPacker, 
                       udpack.XorPtrPosPacker]
    transport, protocol = loop.run_until_complete(udpack.create_udpack(
            loop, 
            functools.partial(udpack.PipelineManager, 
                              pack_pipeline,
                              False),
            ('127.0.0.1', 7000),
            ('127.0.0.1', 7050),
            10, 20))
    transport, protocol = loop.run_until_complete(udpack.create_udpack(
            loop, 
            functools.partial(udpack.PipelineManager, 
                              pack_pipeline,
                              True),
            ('127.0.0.1', 7050),
            ('127.0.0.1', 7100),
            10, 20))
    while True:
        loop.run_until_complete(asyncio.sleep(1))


if __name__ == '__main__':
    main()