#! /usr/bin/env python3

import asyncio
import logging

import random

PACKER_DEFAULT_CONNECT_TIMEOUT = 30
PACKER_DEFAULT_IDLE_TIMEOUT = 60

class UDPackReceiverProtocol(asyncio.DatagramProtocol):
    '''asyncio protocol object responsible for listening from downstream.
    
    Handles connection creation: all packets received from the same (host, port)
    are considered to be one connection, and gets a packer assigned to it.
    The packer manages upstream sockets and connection timeout.'''
    
    def __init__(self, loop, remoteaddr, packer, conf):
        self.logger = logging.getLogger('udpack.receiver')
        self.accesslog = logging.getLogger('access')
        
        self.loop = loop
        self.remoteaddr = remoteaddr
        self.packer = packer
        self.conf = conf
        
        self.connections = {}
    
    def connection_made(self, transport):
        self.logger.debug('Receiver connection_made')
        self.transport = transport
    
    def connection_lost(self, exc):
        if exc is not None:
            self.logger.warning('Receiver connection_lost, exc: {}'.format(exc))
        else:
            self.logger.info('Receiver connection_lost')
        for a in self.connections:
            self.loop.call_soon(self.connections[a].disconnect)
    
    def datagram_received(self, data, addr):
        self.logger.info('Datagram received from {}'.format(addr))
        self.logger.debug('Raw datagram: \n{}'.format(data))
        if addr not in self.connections:
            self.new_connection(addr)
        self.loop.call_soon(self.connections[addr].from_receiver, data)
    
    def error_received(self, exc):
        self.logger.warning('Receiver error_received, exc: {}'.format(exc))
    
    def new_connection(self, addr):
        self.logger.info('Creating new connection from {}'.format(addr))
        self.accesslog.info('New connection from {}'.format(addr))
        newpacker = self.packer(self.loop, self.remoteaddr, self, addr, self.conf)
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
        self.logger.debug('Dispatcher connection_made')
        self.transport = transport
        self.remoteaddr = self.transport.get_extra_info('peername')
    
    def connection_lost(self, exc):
        if exc is not None:
            self.logger.warning('Dispatcher connection_lost, exc: {}'.format(exc))
        else:
            self.logger.info('Dispatcher connection_lost')
    
    def datagram_received(self, data, addr):
        if addr == self.remoteaddr:
            self.logger.info('Datagram received from destination, {}'.format(addr))
            self.logger.debug('Raw datagram: \n{}'.format(data))
            self.loop.call_soon(self.packer.from_dispatcher, data)
        else:
            self.logger.info('Datagram received from non-remote host: {}'.
                    format(addr))
    
    def error_received(self, exc):
        self.logger.warning('Dispatcher error_received, exc: {}'.format(exc))


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
        
        #
        #self.timeout_handle = None
        self.last_from_receiver = self.loop.time()
        self.last_from_dispatcher = self.loop.time()
        self.loop.call_later(1, self.check_timeout)
        
        self.connection_established = False
        self.dispatcher = None
        
        self.dispatcher_task = asyncio.async(self.create_dispatcher())
        self.dispatcher_ready = False
        self.dispatcher_task.add_done_callback(self.set_dispatcher_ready)
        
    def initialize(self, config):
        '''Packer-specific initialization.
        
        Arguments:
        config: the entire configparser object created from the config file.
            Packers should read any required configuration from it.'''
        pass
        
    def check_timeout(self):
        self.logger.debug('Checking timeout status')
        t = self.loop.time()
        if not self.connection_established:
            timeout = self.connect_timeout
        else:
            timeout = self.idle_timeout
            
        if t - self.last_from_receiver > timeout:
            self.logger.info('Packer receiver side timed out')
            self.accesslog.info('Connection from {} timed out: no data received '
                'by receiver for {} seconds'.format(self.receiver_recv_addr,
                    timeout))
            self.disconnect()
            return
        else:
            self.loop.call_later(1, self.check_timeout)
        
    def set_dispatcher_ready(self, future):
        self.logger.debug('Dispatcher is ready')
        self.dispatcher_ready = True
    
    def from_receiver(self, data):
        if not self.dispatcher_ready:
            # Wait until dispatcher is ready
            self.dispatcher_task.add_done_callback(
                lambda future: self.from_receiver(data))
            return
            
        self.logger.debug('Received data from receiver')
        self.last_from_receiver = self.loop.time()
        #self.update_timeout()
        
        self.loop.call_soon(self.process_upstream, data)
    
    def from_dispatcher(self, data):
        self.logger.info('Received data from dispatcher')
        if not self.connection_established:
            self.connection_established = True
            self.logger.info('Bi-directional connection established')
            self.accesslog.info('Received response for connection from {}, '
                'bi-directional connection established'.format(
                    self.receiver_recv_addr))
        self.last_from_dispatcher = self.loop.time()
        #self.update_timeout()
        
        self.loop.call_soon(self.process_downstream, data)
    
    def send_via_dispatcher(self, data):
        self.logger.debug('Sending data via dispatcher')
        self.dispatcher.transport.sendto(data)
    
    def send_via_receiver(self, data):
        self.logger.debug('Sending data via receiver')
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
        
        self.logger.debug('Packing data')
        self.loop.call_soon(send_fn, data)
    
    def unpack(self, data, send_fn):
        '''Deobfuscate data, or do the reverse of whatever pack() does.
        
        Arguments: see pack().
        '''
        
        self.logger.debug('Unpacking data')
        self.loop.call_soon(send_fn, data)
        
    def disconnect(self):
        self.logger.info('Packer disconnecting')
        self.accesslog.info('Connection from {} disconnecting'.format(
                                            self.receiver_recv_addr))
        self.dispatcher.transport.abort()
        self.dispatcher = None
        del self.receiver.connections[self.receiver_recv_addr]
    
    @asyncio.coroutine
    def create_dispatcher(self):
        self.logger.info('Creating dispatcher')
        dispatcher = self.loop.create_datagram_endpoint(
            lambda: UDPackDispatcherProtocol(self.loop, self.remoteaddr, self),
            remote_addr = self.remoteaddr)
        d_transport, d_protocol = yield from dispatcher
        #self.dispatcher_transport = d_transport
        self.dispatcher = d_protocol

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

class UDPackStraightThroughUnpacker(UDPackUnpackerMixIn, UDPackStraightThroughPacker):
    '''Example unpacker. Demonstrates how to inherit from UDPackUnpackerMixIn.
    '''
    pass

class UDPackShufflePacker(UDPackStraightThroughPacker):
    '''Packer that shuffles data byte order with a PSRNG.
    
    The PSRNG is seeded by the length of the packet + user selected key.'''
    
    def initialize(self, config):
        self.random_seed_key = config['shuffle'].getint('random_seed_key')
        
        self.shuffle_sequence = {}
        
    def pack(self, data, send_fn):
        self.logger.debug('Shuffling data')
        shuffled = bytes(data[i] for i in self.get_shuffle_sequence(len(data))[0])
        self.loop.call_soon(send_fn, shuffled)
    
    def unpack(self, data, send_fn):
        self.logger.debug('Unshuffling data')
        unshuffled = bytes(data[i] for i in self.get_shuffle_sequence(len(data))[1])
        self.loop.call_soon(send_fn, unshuffled)
    
    def get_shuffle_sequence(self, len):
        if len not in self.shuffle_sequence:
            self.logger.debug('Generating shuffle sequence of length {}'.format(len))
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
    
    LISTEN_ADDRESS = ('127.0.0.1', 9000)
    REMOTE_ADDRESS = ('45.55.154.125', 1194)
    
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
    accesslogger.setLevel(logging.DEBUG)
    
    accesslogconsole = logging.StreamHandler()
    accesslogconsoleformatter = logging.Formatter('[%(asctime)s]%(message)s')
    #accesslogconsole.setFormatter(accesslogconsoleformatter)
    accesslogconsole.setLevel(logging.INFO)
    
    accesslogger.addHandler(accesslogconsole)
    
    if args.access_log is not None:
        accesslogfile = logging.FileHandler(args.access_log)
        accesslogfile.setFormatter(accesslogconsoleformatter)
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
    
    try:
        local_config['packer'] = {
            'straightthroughpacker': UDPackStraightThroughPacker,
            'straightthroughunpacker': UDPackStraightThroughUnpacker,
            'shufflepacker': UDPackShufflePacker,
            'shuffleunpacker': UDPackShuffleUnpacker
            }[local_config['packer'].lower()]
    except KeyError:
        raise RuntimeError('Packer {} not recognized'.format(local_config['packer']))
    
    
    # Create listening connection
    loop = asyncio.get_event_loop()
    receiver = loop.create_datagram_endpoint(
        lambda: UDPackReceiverProtocol(loop, local_config['remote_addr'], 
                local_config['packer'], conffile),
        local_addr = local_config['listen_addr'])
    transport, protocol = loop.run_until_complete(receiver)
    
    # Run until interrupt
    try:
        signal.signal(signal.SIGTERM, sigterm_handler)
        while True:
            # Workaround for Python Issue 23057 in Windows
            # https://bugs.python.org/issue23057
            loop.run_until_complete(asyncio.sleep(0.2))
    except (KeyboardInterrupt, SystemExit) as e:
        logger.info("Received {}".format(repr(e)))
    finally:
        logger.info("Terminating")
        transport.abort()




if __name__ == '__main__':
    main_cli()