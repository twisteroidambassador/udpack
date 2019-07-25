"""Implements Protocol and Manager classes for plug-in packers."""

import asyncio
import functools
import logging
from itertools import tee

__all__ = ['create_udpack', 'PipelineManager']

PACKER_DEFAULT_CONNECT_TIMEOUT = 30
PACKER_DEFAULT_IDLE_TIMEOUT = 60
CHECK_TIMEOUT_INTERVAL = 1


def pairwise(iterable):
    """s -> (s0,s1), (s1,s2), (s2, s3), ..."""
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


class ReceiverProtocol(asyncio.DatagramProtocol):
    """asyncio protocol object responsible for listening from downstream.
    
    Handles connection creation: all packets received from the same (host, port)
    are considered to be one connection, and gets a manager assigned to it.
    The manager handles upstream sockets and connection timeout."""

    def __init__(self, remoteaddr, manager_factory,
                 loop=None, connect_timeout=None, idle_timeout=None):
        self._logger = logging.getLogger('udpack.receiver')
        self._accesslog = logging.getLogger('udpack.access')

        self._remoteaddr = remoteaddr
        self._manager_factory = manager_factory
        # This class does not actually use the following 3 parameters except 
        # passing them to managers, so None is OK here
        self._loop = loop
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._managers = {}
        self._transport = None

    def connection_made(self, transport):
        self._logger.debug('Receiver connection_made')
        self._transport = transport
        self._logger.debug(transport)
        self._logger.debug(transport.get_extra_info('socket'))

    def connection_lost(self, exc):
        if exc is not None:
            self._logger.warning('Receiver connection_lost, exc: %s', exc, exc_info=True)
        else:
            self._logger.debug('Receiver connection_lost')
        for m in list(self._managers.values()):
            m.close()

    def datagram_received(self, data, addr):
        self._logger.debug('Datagram received from %s', addr)
        # self._logger.debug('Raw datagram: \n%r', data)
        if addr not in self._managers:
            self._new_manager(addr)
        self._managers[addr].datagram_from_receiver(data)

    def error_received(self, exc):
        self._logger.warning('Receiver error_received, exc: %s', exc, exc_info=True)

    def _new_manager(self, addr):
        """Create a new connection."""
        self._logger.info('Creating new connection from %s', addr)
        self._accesslog.warning('New connection from %s', addr)
        manager = self._manager_factory(
            self._remoteaddr,
            self._transport_sendto_cb_factory(addr),
            self._manager_closed_cb_factory(addr),
            self._loop, self._connect_timeout, self._idle_timeout)
        self._managers[addr] = manager
        # print(self._managers)

    def _manager_closed_cb_factory(self, addr):
        """Return a callable that removes a closed manager."""

        def manager_closed():
            self._accesslog.warning('Connection from %s closed', addr)
            # print(self._managers)
            del self._managers[addr]

        return manager_closed

    def _transport_sendto_cb_factory(self, addr):
        """Return a callable that sends packets to fixed address."""
        return functools.partial(self._transport.sendto, addr=addr)


class DispatcherProtocol(asyncio.DatagramProtocol):
    """asyncio protocol object responsible for listening from upstream.
    """

    def __init__(self, received_cb, close_cb, loop=None):
        self._logger = logging.getLogger('udpack.dispatcher')
        self._received_cb = received_cb
        self._close_cb = close_cb
        self._loop = loop or asyncio.get_event_loop()
        self._transport = None
        self._remoteaddr = None

    def connection_made(self, transport):
        self._logger.info('Dispatcher connection_made')
        self._transport = transport
        self._remoteaddr = transport.get_extra_info('peername')

    def connection_lost(self, exc):
        if exc is not None:
            self._logger.warning('Dispatcher connection_lost, exc: %s', exc, exc_info=True)
        else:
            self._logger.info('Dispatcher connection_lost')
        self._close_cb()

    def datagram_received(self, data, addr):
        if addr == self._remoteaddr:
            self._logger.debug('Datagram received from destination, %s', addr)
            # self._logger.debug('Raw datagram: \n%r', data)
            self._received_cb(data)
        else:
            self._logger.info('Datagram received from non-remote host: %s', addr)

    def error_received(self, exc):
        self._logger.warning('Dispatcher error_received, exc: %s', exc, exc_info=True)


class BaseManager:
    """Base class for Managers.
    
    Managers are responsible for setting up the upstream transport &
    protocol, initializing packers, and handling incoming packets from
    upstream and downstream.
    """

    def __init__(self, remoteaddr, downstream_sendto_cb, close_cb,
                 loop=None,
                 connect_timeout=None,
                 idle_timeout=None):
        """Initialize Manager.
        
        downstream_sendto_cb: callback used to send packets downstream.
        
        close_cb: callback used to signify manager closing. Will not be called
            if explicitly specified to be None.
        
        connect_timeout: if no packets ever arrived from upstream, and no
            packets arrived from downstream for connect_timeout seconds,
            the manager will be closed.
        
        idle_timeout: if there have been packets arriving from upstream, and no
            packets arrived from downstream for idle_timeout sconds, the 
            manager will be closed.
        """
        self._logger = logging.getLogger('udpack.manager')
        self._logger.debug('Manager __init__')

        self._remoteaddr = remoteaddr
        self._downstream_sendto_cb = downstream_sendto_cb
        self._close_cb = close_cb
        self._loop = loop or asyncio.get_event_loop()
        self._connect_timeout = (PACKER_DEFAULT_CONNECT_TIMEOUT
                                 if connect_timeout is None
                                 else connect_timeout)
        self._idle_timeout = (PACKER_DEFAULT_IDLE_TIMEOUT
                              if idle_timeout is None
                              else idle_timeout)
        self._timeout_check_handle = None
        self._last_from_dispatcher = None
        self._last_from_receiver = None
        self._connection_established = False
        self._is_closing = False
        self._dispatcher_protocol = None
        self._dispatcher_transport = None
        self._dispatcher_task = self._loop.create_task(self._create_dispatcher())

    @asyncio.coroutine
    def _create_dispatcher(self):
        self._logger.debug('Creating dispatcher')
        try:
            transport, protocol = yield from self._loop.create_datagram_endpoint(
                functools.partial(DispatcherProtocol,
                                  self.datagram_from_dispatcher,
                                  self.close,
                                  loop=self._loop),
                remote_addr=self._remoteaddr)
        except OSError:
            self._logger.warning('Creating dispatcher failed', exc_info=True)
            self.close()
            return
        self._dispatcher_protocol = protocol
        self._dispatcher_transport = transport
        self._last_from_receiver = self._loop.time()
        self._timeout_check_handle = self._loop.call_later(
            CHECK_TIMEOUT_INTERVAL, self._check_timeout)
        self._logger.debug('Dispatcher ready')

    def _check_timeout(self):
        self._logger.debug('Checking timeout status')
        t = self._loop.time()
        if not self._connection_established:
            timeout = self._connect_timeout
        else:
            timeout = self._idle_timeout
        if t - self._last_from_receiver > timeout:
            self._logger.info('Packer receiver side timed out: no data received for %d seconds', timeout)
            self.close()
            return
        else:
            self._timeout_check_handle = self._loop.call_later(
                CHECK_TIMEOUT_INTERVAL, self._check_timeout)

    def close(self):
        if self._is_closing:
            return
        self._logger.info('Closing manager')
        self._is_closing = True
        if self._timeout_check_handle is not None:
            self._timeout_check_handle.cancel()
        self._dispatcher_task.cancel()
        if self._dispatcher_transport is not None:
            self._dispatcher_transport.close()
        if self._close_cb is not None:
            self._close_cb()

    def datagram_from_receiver(self, data):
        """Called by receiver when packets arrive from downstream."""
        if self._is_closing:
            self._logger.debug('Received datagram from receiver, but _is_closing')
            return
        self._last_from_receiver = self._loop.time()
        self._logger.debug('Received datagram from receiver')
        self._process_upstream(data)

    def _datagram_to_dispatcher(self, data):
        """Send packet upstream via dispatcher."""
        if self._is_closing:
            self._logger.debug('Sending datagram to dispatcher, but _is_closing')
            return
        if self._dispatcher_transport is not None:
            self._logger.debug('Sending datagram to dispatcher')
            self._dispatcher_transport.sendto(data)
        else:
            self._logger.debug('Scheduling sending datagram to dispatcher when it becomes ready')
            self._dispatcher_task.add_done_callback(
                lambda fut: self._datagram_to_dispatcher(data))

    def datagram_from_dispatcher(self, data):
        """Called by dispatcher when packets arrive from upstream."""
        if self._is_closing:
            self._logger.debug('Received datagram from dispatcher, but _is_closing')
            return
        self._last_from_dispatcher = self._loop.time()
        self._logger.debug('Received data from dispatcher')
        if not self._connection_established:
            self._connection_established = True
            self._logger.info('Bi-directional connection established')
        self._process_downstream(data)

    def _datagram_to_receiver(self, data):
        """Send packet downstream via receiver."""
        if self._is_closing:
            self._logger.debug('Sending datagram to receiver, but _is_closing')
            return
        self._logger.debug('Sending datagram to receiver')
        self._downstream_sendto_cb(data)

    def _process_upstream(self, data):
        """Process packets in the upstream direction.
        
        data: contents of a packet arriving from downstream.
        
        Use self._datagram_to_dispatcher() to send processed packets upstream.
        """
        raise NotImplementedError

    def _process_downstream(self, data):
        """Process packets in the downstream direction.
        
        data: contents of a packet arriving from upstream.
        
        Use self._datagram_to_receiver() to send processed packets downstream.
        """
        raise NotImplementedError


class PipelineManager(BaseManager):
    """Use a sequence of packers to process packets.
    
    When reverse=False, packets going upstream are processed by 
    packer[0].pack(), then by packer[1].pack(), packer[2].pack(), etc. Packets 
    going downstream are processed by packer[n].unpack(), packer[n-1].unpack(),
    etc. When reverse=True, packets going upstream are unpacked and those going
    downstream are packed.
    """

    def __init__(self, packer_factories, reverse,
                 *args, **kwargs):
        """Initialize Manager.
        
        packer_factories: a sequence of factory functions that produce packers.
        
        reverse: when False, packets going upstream are pack()ed and those going
            downstream are unpack()ed. When True, upstream => unpack() and
            downstream => pack().
        """
        super().__init__(*args, **kwargs)
        pipeline = [p() for p in packer_factories]
        for a, b in pairwise(pipeline):
            a.packed_cb = b.pack
            b.unpacked_cb = a.unpack
        if not reverse:
            self._process_upstream = pipeline[0].pack
            pipeline[-1].packed_cb = self._datagram_to_dispatcher
            self._process_downstream = pipeline[-1].unpack
            pipeline[0].unpacked_cb = self._datagram_to_receiver
        else:
            self._process_upstream = pipeline[-1].unpack
            pipeline[0].unpacked_cb = self._datagram_to_dispatcher
            self._process_downstream = pipeline[0].pack
            pipeline[-1].packed_cb = self._datagram_to_receiver


def create_udpack(loop, manager_factory, local_addr, remote_addr,
                  connect_timeout=None, idle_timeout=None, **kwargs):
    """Create a UDPack instance using the given manager_factory.
    
    Returns the coroutine object returned by loop.create_datagram_endpoint(), 
    which can be yielded from to get a (transport, protocol) pair.
    
    manager_factory: a callable that returns a Manager instance.
    local_addr: address to listen on.
    remote_addr: address to forward mangled packets to.
    
    All other keyword arguments are passed to loop.create_datagram_endpoint().
    """
    protocol = functools.partial(ReceiverProtocol, remote_addr, manager_factory,
                                 loop, connect_timeout, idle_timeout)
    return loop.create_datagram_endpoint(protocol, local_addr=local_addr, **kwargs)
