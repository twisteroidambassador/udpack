#! /usr/bin/env python3

import asyncio
import logging

# Used in UDPackShufflePacker, UDPackToyModelEncryptionPacker
import random
# Used in UDPackXORPatchPacker
import math
# Used in UDPackToyModelEncryptionPacker
import hmac

PACKER_DEFAULT_CONNECT_TIMEOUT = 30
PACKER_DEFAULT_IDLE_TIMEOUT = 60

CHECK_TIMEOUT_INTERVAL = 1

class UDPackReceiverProtocol(asyncio.DatagramProtocol):
    '''asyncio protocol object responsible for listening from downstream.
    
    Handles connection creation: all packets received from the same (host, port)
    are considered to be one connection, and gets a packer assigned to it.
    The packer manages upstream sockets and connection timeout.'''
    
    def __init__(self, loop, remoteaddr, packer, conf, 
            connect_timeout = PACKER_DEFAULT_CONNECT_TIMEOUT,
            idle_timeout = PACKER_DEFAULT_IDLE_TIMEOUT):
        self.logger = logging.getLogger('udpack.receiver')
        self.accesslog = logging.getLogger('access')
        
        self.loop = loop
        self.remoteaddr = remoteaddr
        self.packer = packer
        self.conf = conf
        
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        
        self.connections = {}
    
    def connection_made(self, transport):
        self.logger.info('Receiver connection_made')
        self.transport = transport
    
    def connection_lost(self, exc):
        if exc is not None:
            self.logger.warning('Receiver connection_lost, exc: %s', exc, exc_info=True)
        else:
            self.logger.info('Receiver connection_lost')
        for a in self.connections:
            self.loop.call_soon(self.connections[a].disconnect)
    
    def datagram_received(self, data, addr):
        self.logger.info('Datagram received from %s', addr)
        self.logger.debug('Raw datagram: \n%r', data)
        if addr not in self.connections:
            self.new_connection(addr)
        self.loop.call_soon(self.connections[addr].from_receiver, data)
    
    def error_received(self, exc):
        self.logger.warning('Receiver error_received, exc: %s', exc, exc_info=True)
    
    def new_connection(self, addr):
        self.logger.info('Creating new connection from %s', addr)
        self.accesslog.info('New connection from %s', addr)
        newpacker = self.packer(self.loop, self.remoteaddr, self, addr, 
                self.conf, self.connect_timeout, self.idle_timeout)
        self.connections[addr] = newpacker
    
    
class UDPackDispatcherProtocol(asyncio.DatagramProtocol):
    '''asyncio protocol object responsible for listening from upstream.
    '''
    def __init__(self, loop, remoteaddr, packer):
        self.logger = logging.getLogger('udpack.dispatcher')
        
        self.loop = loop
        #self.remoteaddr = remoteaddr
        self.packer = packer
    
    def connection_made(self, transport):
        self.logger.info('Dispatcher connection_made')
        self.transport = transport
        self.remoteaddr = self.transport.get_extra_info('peername')
    
    def connection_lost(self, exc):
        if exc is not None:
            self.logger.warning('Dispatcher connection_lost, exc: %s', exc, exc_info=True)
        else:
            self.logger.info('Dispatcher connection_lost')
    
    def datagram_received(self, data, addr):
        if addr == self.remoteaddr:
            self.logger.info('Datagram received from destination, %s', addr)
            self.logger.debug('Raw datagram: \n%r', data)
            self.loop.call_soon(self.packer.from_dispatcher, data)
        else:
            self.logger.info('Datagram received from non-remote host: %s', addr)
    
    def error_received(self, exc):
        self.logger.warning('Dispatcher error_received, exc: %s', exc, exc_info=True)


class UDPackStraightThroughPacker():
    '''Basic packer which does not modify packets. Other packers should inherit from this.
    
    Apart from modifying packets, this object also handles connection timeout.
    
    In most cases, in order to implement a new packer, just override the 
    following functions:
        initialize()
        pack()
        unpack()
    '''
    
    def __init__(self, loop, remoteaddr, receiver_protocol, receiver_recv_addr,
            config = None,
            connect_timeout = PACKER_DEFAULT_CONNECT_TIMEOUT,
            idle_timeout = PACKER_DEFAULT_IDLE_TIMEOUT):
        
        self.logger = logging.getLogger('udpack.packer')
        self.accesslog = logging.getLogger('access')
        self.logger.debug('Packer __init__')
        
        self.loop = loop
        self.remoteaddr = remoteaddr
        self.receiver = receiver_protocol
        self.receiver_recv_addr = receiver_recv_addr
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        
        self.initialize(config)
        
        self.last_from_receiver = self.loop.time()
        self.last_from_dispatcher = self.loop.time()
        
        self.timeout_check_handle = None
        #self.loop.call_later(CHECK_TIMEOUT_INTERVAL, self.check_timeout)
        
        self.connection_established = False
        self.dispatcher = None
        
        self.dispatcher_task = asyncio.async(self.create_dispatcher())
        #self.dispatcher_ready = False
        self.dispatcher_task.add_done_callback(self.set_dispatcher_ready)
        
    def initialize(self, config):
        '''Packer-specific initialization.
        
        Arguments:
        config: a dictionary (or configparser object created from the config 
            file). Packers should read any required configuration from it.'''
        assert not hasattr(super(), 'initialize')
        
    def check_timeout(self):
        self.logger.debug('Checking timeout status')
        t = self.loop.time()
        if not self.connection_established:
            timeout = self.connect_timeout
        else:
            timeout = self.idle_timeout
            
        if t - self.last_from_receiver > timeout:
            self.logger.info('Packer receiver side timed out')
            self.accesslog.info('Connection from %s timed out: no data received '
                'by receiver for %d seconds', self.receiver_recv_addr, timeout)
            self.disconnect()
            return
        else:
            self.timeout_check_handle = self.loop.call_later(CHECK_TIMEOUT_INTERVAL, self.check_timeout)
        
    def set_dispatcher_ready(self, future):
        self.dispatcher_task = None
        try:
            transport, protocol = future.result()
            self.dispatcher = protocol
            self.logger.info('Dispatcher is ready')
            
            self.timeout_check_handle = self.loop.call_later(CHECK_TIMEOUT_INTERVAL, self.check_timeout)
        except Exception as e:
            self.logger.warning('Creating dispatcher failed, exception: %s', e, exc_info=True)
            self.disconnect()
    
    def from_receiver(self, data):
        #if not self.dispatcher_ready:
        if self.dispatcher is None:
            if self.dispatcher_task is not None:
                # Wait until dispatcher is ready
                self.logger.info('Received data from receiver but dispatcher '
                    'is not yet ready')
                self.dispatcher_task.add_done_callback(
                    lambda future: self.from_receiver(data))
                return
            else:
                self.logger.warning('Dispatcher does not exist, discarding packet')
                return
            
        self.logger.info('Received data from receiver')
        self.last_from_receiver = self.loop.time()
        #self.update_timeout()
        
        self.loop.call_soon(self.process_upstream, data)
    
    def from_dispatcher(self, data):
        self.logger.info('Received data from dispatcher')
        if not self.connection_established:
            self.connection_established = True
            self.logger.info('Bi-directional connection established')
            self.accesslog.info('Received response for connection from %s, '
                'bi-directional connection established', self.receiver_recv_addr)
        self.last_from_dispatcher = self.loop.time()
        #self.update_timeout()
        
        self.loop.call_soon(self.process_downstream, data)
    
    def send_via_dispatcher(self, data):
        self.logger.info('Sending data via dispatcher')
        self.dispatcher.transport.sendto(data)
    
    def send_via_receiver(self, data):
        self.logger.info('Sending data via receiver')
        self.receiver.transport.sendto(data, self.receiver_recv_addr)
    
    # The following two functions define the direction of the packer / unpacker.
    # UDPackUnpackerMixIn swaps these two functions, so inheriting from that
    # will reverse the direction.
    def process_upstream(self, data):
        self.pack(data, self.send_via_dispatcher)
    
    def process_downstream(self, data):
        self.unpack(data, self.send_via_receiver)
    
    # ====================
    # Modify the two following functions to implement new packing logic
    # ====================
    def pack(self, data, send_fn):
        '''Obfuscate data, or do the reverse of whatever unpack() does.
        
        Arguments:
        data: a bytes object containing the data in a received UDP packet.
        send_fn: a function that should be used to send UDP packets. Takes a 
            single argument of data to be sent.
            Either call it directly: 
                send_fn(data_to_be_sent)
            or schedule it with asyncio: 
                self.loop.call_soon(send_fn, data_to_be_sent)
        '''
        
        self.logger.info('Packing data')
        self.loop.call_soon(send_fn, data)
    
    def unpack(self, data, send_fn):
        '''Deobfuscate data, or do the reverse of whatever pack() does.
        
        Arguments: see pack().
        '''
        
        self.logger.info('Unpacking data')
        self.loop.call_soon(send_fn, data)
        
    def disconnect(self):
        self.logger.info('Packer disconnecting')
        self.accesslog.info('Connection from %s disconnecting', self.receiver_recv_addr)
        if self.timeout_check_handle is not None:
            self.timeout_check_handle.cancel()
        if self.dispatcher is not None:
            self.dispatcher.transport.abort()
            self.dispatcher = None
        del self.receiver.connections[self.receiver_recv_addr]
    
    @asyncio.coroutine
    def create_dispatcher(self):
        self.logger.info('Creating dispatcher')
        dispatcher = self.loop.create_datagram_endpoint(
            lambda: UDPackDispatcherProtocol(self.loop, self.remoteaddr, self),
            remote_addr = self.remoteaddr)
        r = yield from dispatcher
        
        return r

class UDPackUnpackerMixIn():
    '''Make a packer into an unpacker by reversing direction.
    
    Inherit from this and a packer to make an unpacker:
        class UDPackFooBarUnpacker(UDPackUnpackerMixIn, UDPackFooBarPacker):
            pass
    '''
    
    def process_upstream(self, data):
        self.unpack(data, self.send_via_dispatcher)
    
    def process_downstream(self, data):
        self.pack(data, self.send_via_receiver)

class UDPackRandomDelayMixIn():
    '''Delay each obfuscated packet by a random amount.'''
    
    def initialize(self, config):
        random_method = config['randomdelay']['random_method']
        
        if random_method == 'uniform':
            self.random_delay = self.random_delay_uniform
            self.random_min = float(config['randomdelay']['min'])
            self.random_max = float(config['randomdelay']['max'])
        elif random_method == 'exp':
            self.random_delay = self.random_delay_exp
            self.random_mean = float(config['randomdelay']['mean'])
            self.random_max = float(config['randomdelay']['max'])
        
        super().initialize(config)
        
    def random_delay(self):
        '''Randomly choose amount of time to delay packet. 
        
        Replaced by one of the random_delay_* functions in initialize().'''
        
        assert False, 'Execution should not reach here'
        
    def random_delay_uniform(self):
        return random.uniform(self.random_min, self.random_max)
    
    def random_delay_exp(self):
        return max(random.expovariate(1.0 / self.random_mean), self.random_max)
        
    def pack(self, data, send_fn):
        self.logger.info('Scheduling delayed packet')
        super().pack(data, lambda d: self.loop.call_later(
                                self.random_delay(), send_fn, d))

class UDPackStraightThroughUnpacker(UDPackUnpackerMixIn, UDPackStraightThroughPacker):
    '''Example unpacker. Demonstrates how to inherit from UDPackUnpackerMixIn.
    '''
    pass

class UDPackRandomDelayPacker(UDPackRandomDelayMixIn, UDPackStraightThroughPacker):
    '''Delays each packet by a random amount.'''
    pass

class UDPackShufflePacker(UDPackStraightThroughPacker):
    '''Packer that shuffles data byte order with a PRNG.
    
    The PRNG is seeded by the length of the packet + user selected key.'''
    
    
    
    def initialize(self, config):
        self.random_seed_key = int(config['shuffle']['random_seed_key'])
        
        self.shuffle_sequence = {}
        
        super().initialize(config)
        
    def pack(self, data, send_fn):
        self.logger.info('Shuffling data')
        shuffled = bytes(data[i] for i in self.get_shuffle_sequence(len(data))[0])
        self.loop.call_soon(send_fn, shuffled)
    
    def unpack(self, data, send_fn):
        self.logger.info('Unshuffling data')
        unshuffled = bytes(data[i] for i in self.get_shuffle_sequence(len(data))[1])
        self.loop.call_soon(send_fn, unshuffled)
    
    def get_shuffle_sequence(self, len):
        if len not in self.shuffle_sequence:
            self.logger.debug('Generating shuffle sequence of length %d', len)
            random.seed(len + self.random_seed_key)
            s = list(range(len))
            random.shuffle(s)
            s2 = list(enumerate(s))
            s2.sort(key = lambda i: i[1])
            s3 = [i[0] for i in s2]
            self.shuffle_sequence[len] = [s, s3]
        return self.shuffle_sequence[len]

class UDPackShuffleUnpacker(UDPackUnpackerMixIn, UDPackShufflePacker):
    pass

class UDPackXORPatchPacker(UDPackStraightThroughPacker):
    '''Packer that emulates the "XOR patch" available for OpenVPN.
    
    https://tunnelblick.net/cOpenvpn_xorpatch.html
    '''
    
    
    
    def initialize(self, config):
        m = config['xorpatch']['method'].lower()
        if m == 'xormask':
            self.xormask = bytes(config['xorpatch']['xormask'], encoding='utf-8')
            self.scramble = self.scramble_xormask
            self.unscramble = self.scramble_xormask
        elif m == 'reverse':
            self.scramble = self.scramble_reverse
            self.unscramble = self.scramble_reverse
        elif m == 'xorptrpos':
            self.scramble = self.scramble_xorptrpos
            self.unscramble = self.scramble_xorptrpos
        elif m == 'obfuscate':
            self.xormask = bytes(config['xorpatch']['xormask'], encoding='utf-8')
            self.scramble = self.scramble_obfuscate
            self.unscramble = self.unscramble_obfuscate
        else:
            raise RuntimeError('Invalid scramble method "{}"'.format(m))
        
        super().initialize(config)
    
    def pack(self, data, send_fn):
        self.loop.call_soon(send_fn, self.scramble(data))
    
    def unpack(self, data, send_fn):
        self.loop.call_soon(send_fn, self.unscramble(data))
    
    def xor_buffer_mask(self, data, mask):
        '''Byte-wise XOR between data and mask (repeated if necessary).
        
        data, mask should be bytes or bytearrays.'''
        
        mask_pad = math.ceil(len(data) / len(mask)) * mask
        return bytes(a^b for a,b in zip(data, mask_pad))
    
    def xor_ptr_pos(self, data):
        '''XOR each byte with its (1-based) position.
        '''
        return bytes( ((i+1) & 255) ^ d for i,d in enumerate(data))
    
    def reverse_1plus(self, data):
        '''Reverse data[1:].
        '''
        
        if len(data) <= 2:
            return data
        else:
            d = bytearray(data)
            d[1:] = d[:0:-1]
            return bytes(d)
    
    def scramble(self, data):
        '''Placeholder function for scrambling data. Replaced by one of the 
        scramble_* functions during initialize().
        '''
        
        assert False, 'Execution should not reach here'
    
    def unscramble(self, data):
        '''Placeholder function for unscrambling data. Replaced by one of the 
        scramble_* functions during initialize().
        '''
        
        assert False, 'Execution should not reach here'
    
    def scramble_xormask(self, data):
        return self.xor_buffer_mask(data, self.xormask)
    
    def scramble_reverse(self, data):
        return self.reverse_1plus(data)
    
    def scramble_xorptrpos(self, data):
        return self.xor_ptr_pos(data)
    
    def scramble_obfuscate(self, data):
        d = self.xor_ptr_pos(data)
        d = self.reverse_1plus(d)
        d = self.xor_ptr_pos(d)
        d = self.xor_buffer_mask(d, self.xormask)
        return d
    
    def unscramble_obfuscate(self, data):
        d = self.xor_buffer_mask(data, self.xormask)
        d = self.xor_ptr_pos(d)
        d = self.reverse_1plus(d)
        d = self.xor_ptr_pos(d)
        return d

class UDPackXORPatchUnpacker(UDPackUnpackerMixIn, UDPackXORPatchPacker):
    pass

class UDPackToyModelEncryptionPacker(UDPackStraightThroughPacker):
    '''This packer pads and "encrypts" each individual packet.
    
    It uses a toy model "encrypt then authenticate" scheme, with the Mersenne 
    Twister PRNG standing in as a stream cipher, and a truncated SHA-1 HMAC. 
    Of course, this "encryption" scheme is extremely weak and would not stand
    up to any cryptoanalysis. It is designed to have minimal overhead (6 bytes),
    and to randomize the entire packet to hide any signatures.
    
    The plaintext packet is also padded to a random length to thwart analysis 
    of packet length. This adds another 2-byte-minimum overhead.
    '''
    
    IV_LENGTH = 2
    HMAC_LENGTH = 4
    SIZE_LENGTH = 2
    
    
    class HMACCheckFailedException(Exception):
        pass
    
    def initialize(self, config):
        self.random_seed_key = int(config['toymodelenc']['random_seed_key'])
        self.hmac_key = config['toymodelenc']['hmac_key'].encode(encoding='utf-8')
        
        random_method = config['toymodelenc']['random_method'].lower()
        
        if random_method == 'off':
            self.get_random_length = self.get_random_length_disabled
        elif random_method == 'uniform':
            self.get_random_length = self.get_random_length_uniform
            self.random_uniform_min = int(config['toymodelenc']['random_uniform_min'])
            self.random_uniform_max = int(config['toymodelenc']['random_uniform_max'])
        elif random_method == 'triangle':
            self.get_random_length = self.get_random_length_triangle
            self.random_triangle_min = float(config['toymodelenc']['random_triangle_min'])
            self.random_triangle_max = float(config['toymodelenc']['random_triangle_max'])
            self.random_triangle_mode = float(config['toymodelenc']['random_triangle_mode'])
        elif random_method == 'list':
            self.get_random_length = self.get_random_length_list
            l = config['toymodelenc']['random_list']
            if isinstance(l, str):
                l = l.split(',')
            self.random_list = list(map(int, l))
        elif random_method == 'gaussian':
            self.get_random_length = self.get_random_length_normal
            self.random_gauss_mu = float(config['toymodelenc']['random_gauss_mu'])
            self.random_gauss_sigma = float(config['toymodelenc']['random_gauss_sigma'])
            self.random_gauss_min = float(config['toymodelenc']['random_gauss_min'])
            self.random_gauss_max = float(config['toymodelenc']['random_gauss_max'])
        else:
            raise RuntimeError('Invalid random_method "{}"'.format(random_method))
        
        super().initialize(config)
    
    def gen_bytestream(self, iv, key, length):
        '''Generate a byte stream using Python's Random module.'''
        
        randstate = random.getstate()
        
        random.seed(iv + key)
        bs = random.getrandbits(length * 8).to_bytes(length, byteorder='big')
        
        random.setstate(randstate)
        
        return bs
    
    def compute_HMAC(self, data):
        '''Calculate HMAC of data and truncate to required length.'''
        
        return hmac.new(self.hmac_key, data, 'sha1').digest()[:self.HMAC_LENGTH]
    
    def check_HMAC(self, data, hmac_tag):
        '''Chech the truncated HMAC against data.
        
        This is a naive comparison which is certainly vulnerable to timing
        attacks and the like. Do not do this in a serious cryptographic
        application.'''
        
        return hmac_tag == hmac.new(self.hmac_key, data, 'sha1').digest()[:self.HMAC_LENGTH]
    
    def toy_encrypt(self, data, total_length):
        '''Pad, encrypt, and HMAC data.
        
        The name is chosen to really drive home the point that the "encryption" 
        used here is atrouciously weak.'''
        
        self.logger.info('Encrypting data')
        
        pad_length = total_length - len(data) - self.IV_LENGTH \
                     - self.SIZE_LENGTH - self.HMAC_LENGTH
        
        if pad_length < 0:
            # Data too long, everything will not fit inside total_length
            raise RuntimeError('Data of length {} will not fit in total_length {}'.
                    format(len(data), total_length))
        
        iv = random.getrandbits(self.IV_LENGTH * 8)
        plaintext = len(data).to_bytes(self.SIZE_LENGTH, byteorder='big') \
                    + data + bytes(pad_length)
        ciphertext = bytes(a^b for a,b in zip(plaintext, self.gen_bytestream(
                                iv, self.random_seed_key, len(plaintext))))
        
        hmac_data = iv.to_bytes(self.IV_LENGTH, byteorder='big') + ciphertext
        
        hmac_tag = self.compute_HMAC(hmac_data)
        
        return hmac_tag + hmac_data
    
    def toy_decrypt(self, data):
        '''Check HMAC and decrypt data.'''
        
        self.logger.info('Decrypting data')
        
        if not self.check_HMAC(data[self.HMAC_LENGTH:], data[:self.HMAC_LENGTH]):
            raise UDPackToyModelEncryptionPacker.HMACCheckFailedException
        
        iv = int.from_bytes(data[self.HMAC_LENGTH : self.HMAC_LENGTH+self.IV_LENGTH], byteorder='big')
        ciphertext = data[self.HMAC_LENGTH+self.IV_LENGTH:]
        plaintext = bytes(a^b for a,b in zip(ciphertext, self.gen_bytestream(
                                iv, self.random_seed_key, len(ciphertext))))
        
        payload_len = int.from_bytes(plaintext[:self.SIZE_LENGTH], byteorder='big')
        
        return plaintext[self.SIZE_LENGTH : self.SIZE_LENGTH+payload_len]
    
    def get_random_length(self):
        '''Placeholder function for getting a random packet length. Replaced 
        by one of the get_random_length_* functions during initialize().'''
        
        assert False, 'Execution should not reach here'
    
    def get_random_length_disabled(self):
        return 0
    
    def get_random_length_uniform(self):
        return random.choice(range(self.random_uniform_min,
                                   self.random_uniform_max + 1))
        
    def get_random_length_triangle(self):
        return round(random.triangular(self.random_triangle_min,
                        self.random_triangle_max, self.random_triangle_mode))
    
    def get_random_length_list(self):
        return random.choice(self.random_list)
    
    def get_random_length_normal(self):
        r = random.gauss(self.random_gauss_mu, self.random_gauss_sigma)
        r = max(min(r, self.random_gauss_max), self.random_gauss_min)
        return round(r)
        
    def choose_packet_length(self, payload_length):
        return max(self.get_random_length(), 
                   payload_length + self.SIZE_LENGTH + self.IV_LENGTH + self.HMAC_LENGTH)
    
    def pack(self, data, send_fn):
        self.logger.info('Packing')
        l = self.choose_packet_length(len(data))
        packed = self.toy_encrypt(data, l)
        self.loop.call_soon(send_fn, packed)
    
    def unpack(self, data, send_fn):
        self.logger.info('Unpacking')
        try:
            unpacked = self.toy_decrypt(data)
            self.loop.call_soon(send_fn, unpacked)
        except UDPackToyModelEncryptionPacker.HMACCheckFailedException:
            self.logger.warning('Decryption HMAC check failed, possible tampered packet')

class UDPackToyModelEncryptionUnpacker(UDPackUnpackerMixIn, UDPackToyModelEncryptionPacker):
    pass

class UDPackToyModelEncryptionDelayPacker(UDPackRandomDelayMixIn, UDPackToyModelEncryptionPacker):
    pass

class UDPackToyModelEncryptionDelayUnpacker(UDPackRandomDelayMixIn, UDPackUnpackerMixIn, UDPackToyModelEncryptionPacker):
    pass
    
def main_cli():
    import argparse
    import configparser
    import signal, sys
    
    def sigterm_handler(signal, frame):
        sys.exit(0)
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description = 'UDPack is a obfuscating UDP '
            'proxy / relay, generally used in pairs to first obfuscate and '
            'then unobfuscate UDP traffic.', 
            epilog = 'remote-addr, listen-addr and packer can also be '
            'specified in the configuration file. Command line arguments '
            'take precedence.')
    
    parser.add_argument('configfile', type=argparse.FileType('r'), help=
            'Configuration file. Contains packer options.')
    parser.add_argument('--verbose', '-v', action='count', help='Increase ' 
        'verbosity level for application debug log. Specify once to see '
        'WARNING, twice to see INFO and thrice for DEBUG.')
    parser.add_argument('--access-log', '-a', help='Access log filename. '
        'Information on connecting clients will be written to this file, in '
        'addition to being printed to the console.')
    parser.add_argument('--remote-addr', '-r', help='Remote host,port to '
        'connect to. Separate host and port with a comma.')
    parser.add_argument('--listen-addr', '-l', help='Local host,port to listen '
        'on. Separate host and port with a comma.')
    parser.add_argument('--packer', '-p', help='Packer used for processing data.')
    
    args = parser.parse_args()
    
    # Set logging
    logger = logging.getLogger('udpack')
    logger.setLevel(logging.DEBUG)
    
    logconsole = logging.StreamHandler()
    logconsoleformatter = logging.Formatter('[%(asctime)s] %(name)-6s '
            '%(levelname)-8s %(message)s')
    logconsole.setFormatter(logconsoleformatter)
    if args.verbose is None:
        logconsole.setLevel(logging.ERROR)
    elif args.verbose == 1:
        logconsole.setLevel(logging.WARNING)
    elif args.verbose == 2:
        logconsole.setLevel(logging.INFO)
    else:
        logconsole.setLevel(logging.DEBUG)
    
    logger.addHandler(logconsole)
    
    logger.debug("Verbosity level set")
    logger.debug("Arguments:")
    logger.debug(args)
    
    accesslogger = logging.getLogger('access')
    accesslogger.setLevel(logging.INFO)
    
    accesslogconsole = logging.StreamHandler()
    accesslogfileformatter = logging.Formatter('[%(asctime)s]%(message)s')
    accesslogconsole.setFormatter(accesslogfileformatter)
    accesslogconsole.setLevel(logging.INFO)
    
    accesslogger.addHandler(accesslogconsole)
    
    if args.access_log is not None:
        accesslogfile = logging.FileHandler(args.access_log)
        accesslogfile.setFormatter(accesslogfileformatter)
        accesslogfile.setLevel(logging.INFO)
        accesslogger.addHandler(accesslogfile)
    
    # Read config file
    conffile = configparser.ConfigParser(empty_lines_in_values=False)
    conffile.read_file(args.configfile)
    args.configfile.close()
    
    logger.debug('Config file read')
    
    # Collect options for main application
    local_config = {}
    for c in ('remote_addr', 'listen_addr', 'packer'):
        if vars(args)[c] is not None:
            local_config[c] = vars(args)[c]
        elif c in conffile['udpack']:
            local_config[c] = conffile['udpack'][c]
        else:
            raise RuntimeError('Option {} not specified in either command line '
                'options or config file'.format(c))
    
    for c in ('remote_addr', 'listen_addr'):
        local_config[c] = tuple(local_config[c].split(','))
    
    local_config['connect_timeout'] = conffile.getint('udpack', 
            'connect_timeout', fallback=PACKER_DEFAULT_CONNECT_TIMEOUT)
    
    local_config['idle_timeout'] = conffile.getint('udpack',
            'idle_timeout', fallback=PACKER_DEFAULT_IDLE_TIMEOUT)
    
    try:
        local_config['packer'] = {
            'straightthroughpacker': UDPackStraightThroughPacker,
            'straightthroughunpacker': UDPackStraightThroughUnpacker,
            'shufflepacker': UDPackShufflePacker,
            'shuffleunpacker': UDPackShuffleUnpacker,
            'xorpatchpacker': UDPackXORPatchPacker,
            'xorpatchunpacker': UDPackXORPatchUnpacker,
            'toymodelencryptionpacker': UDPackToyModelEncryptionPacker,
            'toymodelencryptionunpacker': UDPackToyModelEncryptionUnpacker,
            'toymodelencryptiondelaypacker': UDPackToyModelEncryptionDelayPacker,
            'toymodelencryptiondelayunpacker': UDPackToyModelEncryptionDelayUnpacker
            }[local_config['packer'].lower()]
    except KeyError:
        raise RuntimeError('Invalid packer "{}"'.format(local_config['packer']))
    
    
    # Create listening connection
    loop = asyncio.get_event_loop()
    receiver = loop.create_datagram_endpoint(
        lambda: UDPackReceiverProtocol(loop, local_config['remote_addr'], 
                local_config['packer'], conffile,
                local_config['connect_timeout'],
                local_config['idle_timeout']),
        local_addr = local_config['listen_addr'])
    transport, protocol = loop.run_until_complete(receiver)
    
    # Run until interrupt
    try:
        signal.signal(signal.SIGTERM, sigterm_handler)
        while True:
            # Workaround for Python Issue 23057 in Windows
            # https://bugs.python.org/issue23057
            loop.run_until_complete(asyncio.sleep(1))
    except (KeyboardInterrupt, SystemExit) as e:
        logger.info("Received {}".format(repr(e)))
    finally:
        logger.info("Terminating")
        transport.abort()




if __name__ == '__main__':
    main_cli()
