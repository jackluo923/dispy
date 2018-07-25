#!/usr/bin/python3

"""
dispynetrelay: Relay ping messages from client(s) to nodes
in a network; see accompanying 'dispy' for more details.
"""

import sys
import os
import socket
import traceback
import struct
import time

import dispy
import pycos
from pycos import Task, serialize, deserialize, AsyncSocket

__author__ = "Giridhar Pemmasani (pgiri@yahoo.com)"
__email__ = "pgiri@yahoo.com"
__copyright__ = "Copyright 2011, Giridhar Pemmasani"
__contributors__ = []
__maintainer__ = "Giridhar Pemmasani (pgiri@yahoo.com)"
__license__ = "Apache 2.0"
__url__ = "http://dispy.sourceforge.net"
__status__ = "Production"
__version__ = dispy._dispy_version
__all__ = []

logger = pycos.Logger('dispynetrelay')


class DispyNetRelay(object):
    """Internal use only.
    """

    def __init__(self, ip_addrs=[], node_port=51348, listen_port=0,
                 scheduler_nodes=[], scheduler_port=51347, ipv4_udp_multicast=False,
                  secret='', certfile=None, keyfile=None):
        addrinfos = []
        if not ip_addrs:
            ip_addrs = [None]
        for i in range(len(ip_addrs)):
            ip_addr = ip_addrs[i]
            addrinfo = dispy.host_addrinfo(host=ip_addr)
            if not addrinfo:
                logger.warning('Ignoring invalid ip_addr %s', ip_addr)
                continue
            addrinfos.append(addrinfo)

        self.node_port = node_port
        if not listen_port:
            listen_port = node_port
        self.listen_port = listen_port
        self.ipv4_udp_multicast = bool(ipv4_udp_multicast)
        self.ip_addrs = set()
        self.scheduler_ip_addr = None
        self.scheduler_port = scheduler_port
        self.secret = secret
        if certfile:
            self.certfile = os.path.abspath(certfile)
        else:
            self.certfile = None
        if keyfile:
            self.keyfile = os.path.abspath(keyfile)
        else:
            self.keyfile = None

        udp_addrinfos = {}
        for addrinfo in addrinfos:
            self.ip_addrs.add(addrinfo.ip)
            if addrinfo.family == socket.AF_INET and self.ipv4_udp_multicast:
                addrinfo.broadcast = dispy.IPV4_MULTICAST_GROUP
            Task(self.listen_tcp_proc, addrinfo)
            if os.name == 'nt':
                bind_addr = addrinfo.ip
            elif sys.platform == 'darwin':
                if addrinfo.family == socket.AF_INET and (not self.ipv4_udp_multicast):
                    bind_addr = ''
                else:
                    bind_addr = addrinfo.broadcast
            else:
                bind_addr = addrinfo.broadcast
            udp_addrinfos[bind_addr] = addrinfo

        scheduler_ip_addrs = []
        for addr in scheduler_nodes:
            addr = dispy._node_ipaddr(addr)
            if addr:
                scheduler_ip_addrs.append(addr)

        for bind_addr, addrinfo in udp_addrinfos.items():
            Task(self.listen_udp_proc, bind_addr, addrinfo)
            Task(self.sched_udp_proc, bind_addr, addrinfo)
            for addr in scheduler_ip_addrs:
                msg = {'version': __version__, 'ip_addrs': [addr], 'port': self.scheduler_port,
                       'sign': None}
                Task(self.verify_broadcast, addrinfo, msg)

        logger.info('version %s started', dispy._dispy_version)

    def verify_broadcast(self, addrinfo, msg, task=None):
        if msg.get('relay', None):
            raise StopIteration
        msg['relay'] = 'y'
        # TODO: check if current scheduler is done with nodes?
        if msg['sign']:
            msg['auth'] = dispy.auth_code(self.secret, msg['sign'])
        reply = None
        for scheduler_ip_addr in msg['ip_addrs']:
            msg['scheduler_ip_addr'] = scheduler_ip_addr
            sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_STREAM),
                               keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(dispy.MsgTimeout)
            try:
                yield sock.connect((scheduler_ip_addr, msg['port']))
                yield sock.send_msg('RELAY_INFO:'.encode() + serialize(msg))
                reply = yield sock.recv_msg()
                reply = deserialize(reply)
            except:
                continue
            else:
                break
            finally:
                sock.close()

        if not reply:
            raise StopIteration

        # TODO: since dispynetrelay is not aware of computations closing, if
        # more than one client sends ping, nodes will respond to different
        # clients
        self.scheduler_ip_addr = reply['ip_addrs'] = [scheduler_ip_addr]
        self.scheduler_port = reply['port']
        bc_sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_DGRAM))
        ttl_bin = struct.pack('@i', 1)
        if addrinfo.family == socket.AF_INET:
            if self.ipv4_udp_multicast:
                bc_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl_bin)
            else:
                bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        else:  # addrinfo.family == socket.AF_INET6
            bc_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, ttl_bin)
            bc_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, addrinfo.ifn)
        bc_sock.bind((addrinfo.ip, 0))
        yield bc_sock.sendto('PING:'.encode() + serialize(msg),
                             (addrinfo.broadcast, self.node_port))
        bc_sock.close()

    def listen_udp_proc(self, bind_addr, addrinfo, task=None):
        task.set_daemon()

        listen_sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_DGRAM))
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        listen_sock.bind((bind_addr, self.listen_port))

        if addrinfo.family == socket.AF_INET:
            if self.ipv4_udp_multicast:
                mreq = socket.inet_aton(addrinfo.broadcast) + socket.inet_aton(addrinfo.ip)
                listen_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        else:  # addrinfo.family == socket.AF_INET6:
            mreq = socket.inet_pton(addrinfo.family, addrinfo.broadcast)
            mreq += struct.pack('@I', addrinfo.ifn)
            listen_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
            try:
                listen_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except:
                pass

        while 1:
            msg, addr = yield listen_sock.recvfrom(1024)
            if not msg.startswith('PING:'.encode()):
                logger.debug('Ignoring message from %s', addr[0])
                continue
            if addr[0] in self.ip_addrs:
                logger.debug('Ignoring loop back ping from %s' % addr[0])
                continue
            try:
                msg = deserialize(msg[len('PING:'.encode()):])
                if msg['version'] != __version__:
                    logger.warning('Ignoring %s due to version mismatch: %s / %s',
                                   msg['ip_addrs'], msg['version'], __version__)
                    continue
            except:
                logger.debug('Ignoring ping message from %s (%s)', addr[0], addr[1])
                logger.debug(traceback.format_exc())
                continue
            Task(self.verify_broadcast, addrnifo, msg)

    def listen_tcp_proc(self, addrinfo, task=None):
        task.set_daemon()
        auth_len = len(dispy.auth_code('', ''))
        tcp_sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_STREAM),
                                   keyfile=self.keyfile, certfile=self.certfile)
        tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_sock.bind((addrinfo.ip, self.listen_port))
        tcp_sock.listen(8)

        def tcp_req(conn, addr, task=None):
            conn.settimeout(dispy.MsgTimeout)
            try:
                msg = yield conn.recvall(auth_len)
                msg = yield conn.recv_msg()
            except:
                logger.debug(traceback.format_exc())
                logger.debug('Ignoring invalid TCP message from %s:%s', addr[0], addr[1])
                raise StopIteration
            finally:
                conn.close()
            try:
                msg = deserialize(msg[len('PING:'.encode()):])
                if msg['version'] != __version__:
                    logger.warning('Ignoring %s due to version mismatch: %s / %s',
                                   msg['ip_addrs'], msg['version'], __version__)
                    raise StopIteration
            except:
                logger.debug('Ignoring ping message from %s (%s)', addr[0], addr[1])
                logger.debug(traceback.format_exc())
                raise StopIteration
            Task(self.verify_broadcast, addrinfo, msg)

        while 1:
            conn, addr = yield tcp_sock.accept()
            Task(tcp_req, conn, addr)

    def sched_udp_proc(self, bind_addr, addrinfo, task=None):
        task.set_daemon()

        def relay_msg(msg, task=None):
            relay = {'ip_addrs': self.scheduler_ip_addr, 'port': self.scheduler_port,
                     'version': __version__}
            relay['relay'] = 'y'
            sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_STREAM),
                               keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(dispy.MsgTimeout)
            yield sock.connect((msg['ip_addr'], msg['port']))
            yield sock.sendall(dispy.auth_code(self.secret, msg['sign']))
            yield sock.send_msg('PING:'.encode() + serialize(relay))
            sock.close()

        sched_sock = AsyncSocket(socket.socket(addrinfo.family, socket.SOCK_DGRAM))
        sched_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            sched_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sched_sock.bind((bind_addr, self.scheduler_port))

        if addrinfo.family == socket.AF_INET:
            if self.ipv4_udp_multicast:
                mreq = socket.inet_aton(addrinfo.broadcast) + socket.inet_aton(addrinfo.ip)
                sched_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        else:  # addrinfo.family == socket.AF_INET6:
            mreq = socket.inet_pton(addrinfo.family, addrinfo.broadcast)
            mreq += struct.pack('@I', addrinfo.ifn)
            sched_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
            try:
                sched_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except:
                pass

        while 1:
            msg, addr = yield sched_sock.recvfrom(1024)
            if not msg.startswith('PING:'.encode()):
                logger.debug('Ignoring message from %s (%s)', addr[0], addr[1])
                continue
            try:
                msg = deserialize(msg[len('PING:'.encode()):])
                assert msg['version'] == __version__
                # assert isinstance(msg['cpus'], int)
            except:
                continue
            if not self.scheduler_ip_addr:
                continue
            Task(relay_msg, msg)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', dest='loglevel', default=False,
                        help='if given, debug messages are printed')
    parser.add_argument('-i', '--ip_addr', dest='ip_addrs', default=[],  action='append',
                        help='IP address to use (may be needed in case of multiple interfaces)')
    parser.add_argument('--scheduler_node', dest='scheduler_nodes', default=[], action='append',
                        help='name or IP address of scheduler to announce when starting')
    parser.add_argument('--scheduler_port', dest='scheduler_port', type=int, default=51347,
                        help='port number used by scheduler')
    parser.add_argument('--node_port', dest='node_port', type=int, default=51348,
                        help='port number used by nodes')
    parser.add_argument('--listen_port', dest='listen_port', type=int, default=0,
                        help='port number to listen (instead of node_port) '
                        'for connection from client')
    parser.add_argument('-s', '--secret', dest='secret', default='',
                        help='authentication secret for handshake with dispy clients')
    parser.add_argument('--certfile', dest='certfile', default='',
                        help='file containing SSL certificate')
    parser.add_argument('--keyfile', dest='keyfile', default='',
                        help='file containing SSL key')
    parser.add_argument('--ipv4_udp_multicast', dest='ipv4_udp_multicast', action='store_true',
                        default=False, help='use multicast for IPv4 UDP instead of broadcast')
    parser.add_argument('--daemon', action='store_true', dest='daemon', default=False,
                        help='if given, input is not read from terminal ')
    config = vars(parser.parse_args(sys.argv[1:]))

    if config['loglevel']:
        logger.setLevel(logger.DEBUG)
    else:
        logger.setLevel(logger.INFO)
    del config['loglevel']
    daemon = config.pop('daemon')

    DispyNetRelay(**config)

    try:
        if os.getpgrp() != os.tcgetpgrp(sys.stdin.fileno()):
            daemon = True
    except:
        pass

    if daemon:
        while 1:
            try:
                time.sleep(3600)
            except:
                break
    else:
        while True:
            try:
                cmd = input('Enter "quit" or "exit" to terminate dispynetrelay: ')
                cmd = cmd.strip().lower()
                if cmd == 'quit' or cmd == 'exit':
                    break
            except KeyboardInterrupt:
                # TODO: terminate even if jobs are scheduled?
                logger.info('Interrupted; terminating')
                break
            except:
                logger.debug(traceback.format_exc())
