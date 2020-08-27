"""\
Purpose: Network connection facilities - mock servers
Initial Version: Costas Skarakis 16/2/2020
"""
import selectors
import socket
import threading
import time
import traceback
import types
from copy import copy

import common.client as my_clients
from common import util
from common.tc_logging import debug, warning
from sip.SipParser import parseBytes as parseSip
from csta.CstaApplication import CstaApplication
from csta.CstaParser import parseBytes
from csta.CstaUser import CstaUser
from sip.SipEndpoint import SipEndpoint


# With help from https://github.com/realpython/materials/blob/master/python-sockets-tutorial/multiconn-server.py


class SipServer:
    """
    A simple server to send and receive SIP Messages
    """

    def __init__(self, ip, port, protocol="tcp"):
        self.ip = ip
        self.port = port
        self.protocol = protocol
        self.connections = []
        self.continue_serving = False
        self.sel = selectors.DefaultSelector()
        if protocol in ("tcp", "TCP"):
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        elif protocol in ("udp", "UDP"):
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        elif protocol in ("tls", "TLS"):
            # for MTLS this might be needed
            # context.load_cert_chain('/path/to/certchain.pem', '/path/to/private.key')
            raise NotImplemented
        self.server_thread = None
        self.sip_endpoint = SipEndpoint("PythonSipServer")
        self.handlers = {}
        self.handlers_args = {}
        self.threads = []
        self.events = {}
        self.buffers = {}
        self.reply = self.sip_endpoint.reply
        self.send = self.sip_endpoint.send
        self.wait_for_message = self.sip_endpoint.wait_for_message
        self.wait_for_messages = self.sip_endpoint.wait_for_messages
        self.set_dialog = self.sip_endpoint.set_dialog
        self.save_message = self.sip_endpoint.save_message
        self.set_transaction = self.sip_endpoint.set_transaction

    def accept_wrapper(self, sock):
        conn, addr = sock.accept()  # Should be ready to read
        print("accepted connection from", addr)
        conn.setblocking(False)
        data = types.SimpleNamespace(addr=addr, inb=b"", outb=b"")
        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        self.sel.register(conn, events, data=data)
        self.make_client(conn, addr)

    def make_client(self, sock, addr):
        local_ip, local_port = sock.getsockname()
        client = None
        if self.protocol in ("tcp", "TCP"):
            client = my_clients.TCPClient(local_ip, local_port)
        elif self.protocol in ("udp", "UDP"):
            client = my_clients.UDPClient(local_ip, local_port)
        elif self.protocol in ("tls", "TLS"):
            client = my_clients.TLSClient(local_ip, local_port, None)
        client.rip = addr[0]
        client.rport = addr[1]
        self.sip_endpoint.use_link(client)
        self.sip_endpoint.link.socket = sock
        #        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sip_endpoint.link.sockfile = sock.makefile(mode='rb')

        return client

    def serve_forever(self):
        self.socket.bind((self.ip, self.port))
        self.socket.listen()
        self.port = self.socket.getsockname()[1]
        print("listening on", (self.ip, self.port))
        self.socket.setblocking(False)
        self.sel.register(self.socket, selectors.EVENT_READ, data=None)
        self.continue_serving = True
        while self.continue_serving:
            try:
                events = self.sel.select(timeout=60)
                for key, mask in events:
                    if key.data is None:
                        self.accept_wrapper(key.fileobj)
                    else:
                        self.service_connection(key, mask)
            except socket.timeout:
                print("timeout")
                traceback.print_exc()
                continue
            except (IOError, socket.error):
                print("Disconnected. Waiting to reconnect")
                # self.sel.unregister(self.csta_endpoint.link.socket)
                continue
            except KeyboardInterrupt:
                self.shutdown()
            except:
                debug(traceback.format_exc())
                break
        self.sel.unregister(self.socket)
        # self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        self.sel.close()

    def wait_shutdown(self):
        time.sleep(3)
        for thread in self.threads:
            thread.join()
        self.shutdown()

    def shutdown(self):
        print("Shutting down server. It may take up to 5 seconds")
        self.continue_serving = False
        self.server_thread.join()

    def serve_in_background(self):
        self.server_thread = threading.Thread(target=self.serve_forever)
        self.server_thread.daemon = False
        self.server_thread.start()

    def on(self, incoming_message_type, action, args=()):
        """
        Define a response action to a trigger received message

        :param incoming_message_type: The trigger message
        :param action: A function to call when this message is received.
                    The first argument of this function
        :param args: Optional positional arguments to pass to action starting from 3rd position
        """
        self.handlers[incoming_message_type] = action
        self.handlers_args[incoming_message_type] = args
        self.events[incoming_message_type] = threading.Event()
        self.buffers[incoming_message_type] = []
        t = threading.Thread(target=self.handlers[incoming_message_type],
                             args=(self, *self.handlers_args[incoming_message_type]),
                             daemon=True)
        t.start()
        self.threads.append(t)

    def service_connection(self, key, mask):
        # sock = key.fileobj
        # data = key.data
        if mask & selectors.EVENT_READ:
            try:
                inbytes = self.sip_endpoint.link.waitForSipData(timeout=5.0)
                if inbytes is None:
                    self.sip_endpoint.link.socket.close()
                    self.sel.unregister(self.sip_endpoint.link.socket)
                    return
                inmessage = parseSip(inbytes)
                if inmessage.get_status_or_method() in self.handlers:
                    self.events[inmessage.get_status_or_method()].set()
                    self.buffers[inmessage.get_status_or_method()].append(inmessage)
                else:
                    self.sip_endpoint.message_buffer.append(inmessage)
            except UnicodeDecodeError:
                debug("Ignoring malformed data")
        # if mask & selectors.EVENT_WRITE:
        #     # print("ready to write")
        #     if data.outb:self.events['MonitorStart']
        #         print("echoing", repr(data.outb), "to", data.addr)
        #         sent = sock.send(data.outb)  # Should be ready to write
        #         data.outb = data.outb[sent:]


class CstaServer(SipServer):
    def __init__(self, ip, port, protocol="tcp"):
        super().__init__(ip, port, protocol)
        self.name = "PythonCstaServer"
        self.refid = 0
        self.csta_endpoint = CstaApplication(server=True)
        self.user = self.csta_endpoint.new_user(self.name)
        self.user.parameters["monitorCrossRefID"] = 9999
        self.csta_endpoint.parameters = {self.name: {"eventid": 1}}
        self.wait_for_csta_message = self.csta_endpoint.wait_for_csta_message
        self.wait_for = self.csta_endpoint.wait_for_csta_message
        self.on("MonitorStart", self.monitor_user)
        self.on("SystemStatus", self.system_status)
        self.on("SystemRegister", self.system_register)
        self.on("SnapshotDevice", self.snapshot_device)
        self.lock = threading.Lock()

    def cleanup(self):
        del self.csta_endpoint
        self.csta_endpoint = CstaApplication(server=True)
        self.user = self.csta_endpoint.new_user(self.name)
        self.user.parameters["monitorCrossRefID"] = 9999
        self.csta_endpoint.parameters = {self.name: {"eventid": 1}}
        self.wait_for_csta_message = self.csta_endpoint.wait_for_csta_message
        self.wait_for = self.csta_endpoint.wait_for_csta_message

    def send(self, message, to_user, from_user=None):
        """ We are using the client methods for the server, which make the from_user and to_user
        terms to have opposite values. We use this method to reverse them and make the code make more sense.

        :param to_user: Will be turned to from_user in CstaApplication.send
        :param from_user: Will be turned to to_user
        :param message: The Message type
        :return: The CstaMessage sent
        """
        if isinstance(to_user, CstaUser):
            return to_user.send(message)
        else:
            return self.csta_endpoint.send(from_user=to_user, message=message, to_user=from_user)

    def set_parameter(self, user, key, value):
        self.csta_endpoint.parameters[user][key] = value

    def accept_wrapper(self, sock):
        self.cleanup()
        conn, addr = sock.accept()  # Should be ready to read
        print("accepted connection from", addr)
        conn.setblocking(False)
        data = types.SimpleNamespace(addr=addr, inb=b"", outb=b"")
        events = selectors.EVENT_READ  # | selectors.EVENT_WRITE
        self.connections.append(conn)
        self.make_client(conn, addr)
        self.sel.register(conn, events, data=data)
        self.user.parameters["systemStatus"] = "enabled"
        self.user.parameters["sysStatRegisterID"] = self.name
        self.csta_endpoint.send(from_user=self.name, to_user=None, message="SystemStatus")
        # self.csta_endpoint.wait_for_csta_message(for_user=self.name, message="SystemStatusResponse")
        self.user.parameters["eventid"] = 0

    def make_client(self, sock, addr):
        local_ip, local_port = sock.getsockname()
        client = None
        if self.protocol in ("tcp", "TCP"):
            client = my_clients.TCPClient(local_ip, local_port)
        elif self.protocol in ("udp", "UDP"):
            client = my_clients.UDPClient(local_ip, local_port)
        elif self.protocol in ("tls", "TLS"):
            client = my_clients.TLSClient(local_ip, local_port, None)
        client.rip = addr[0]
        client.rport = addr[1]
        self.csta_endpoint.link = client
        self.csta_endpoint.link.socket = sock
        #        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.csta_endpoint.link.sockfile = sock.makefile(mode='rb')
        return client

    def service_connection(self, key, mask):
        # sock = key.fileobj
        # data = key.data
        if mask & selectors.EVENT_READ:
            try:
                inbytes = self.csta_endpoint.link.waitForCstaData(timeout=5.0)
                if inbytes is None:
                    self.csta_endpoint.link.socket.close()
                    self.sel.unregister(self.csta_endpoint.link.socket)
                    return
                inmessage = parseBytes(inbytes)
                if inmessage.event in self.handlers:
                    self.events[inmessage.event].set()
                    self.buffers[inmessage.event].append(inmessage)
                elif inmessage.event == "SystemStatusResponse":
                    #TODO: Ignoring incoming SystemStatusResponse for now
                    return
                else:
                    self.csta_endpoint.message_buffer.append(inmessage)
            except UnicodeDecodeError:
                debug("Ignoring malformed data")
        # if mask & selectors.EVENT_WRITE:
        #     # print("ready to write")
        #     if data.outb:self.events['MonitorStart']
        #         print("echoing", repr(data.outb), "to", data.addr)
        #         sent = sock.send(data.outb)  # Should be ready to write
        #         data.outb = data.outb[sent:]

    def get_user(self, directory_number):
        return self.csta_endpoint.get_user(directory_number)

    @staticmethod
    def monitor_user(self):
        while True:
            self.events["MonitorStart"].wait()
            self.events["MonitorStart"].clear()
            while True:
                try:
                    monitor_message = self.buffers["MonitorStart"].pop(0)
                    user = monitor_message["deviceObject"]
                    if not user:
                        debug("Invalid monitor user '{}'. Empty deviceID tag.".format(user))
                        return
                    self.csta_endpoint.new_user(user).parameters = {"monitorCrossRefID": self.refid,
                                                                    "deviceID": user,
                                                                    "CSTA_CREATE_MONITOR_CROSS_REF_ID": self.refid,
                                                                    "CSTA_USE_MONITOR_CROSS_REF_ID": self.refid}
                    self.refid += 1
                    self.csta_endpoint.get_user(user).update_incoming_transactions(monitor_message)
                    self.send("MonitorStartResponse", to_user=user)
                    self.csta_endpoint.get_user(user).update_outgoing_transactions(monitor_message)
                except IndexError:
                    break

    @staticmethod
    def system_register(self):
        while True:
            self.events["SystemRegister"].wait()
            self.events["SystemRegister"].clear()
            while True:
                try:
                    user = self.csta_endpoint.get_user(self.name)
                    message = self.buffers["SystemRegister"].pop(0)
                    user.update_incoming_transactions(message)
                    self.csta_endpoint.parameters[self.name]["sysStatRegisterID"] = self.name
                    self.csta_endpoint.send(from_user=self.name, to_user=None, message="SystemRegisterResponse")
                    user.update_outgoing_transactions(message)
                except IndexError:
                    break

    @staticmethod
    def system_status(self):
        while True:
            self.events["SystemStatus"].wait()
            self.events["SystemStatus"].clear()
            while True:
                try:
                    user = self.csta_endpoint.get_user(self.name)
                    message = self.buffers["SystemStatus"].pop(0)
                    user.update_incoming_transactions(message)
                    self.csta_endpoint.parameters[self.name]["systemStatus"] = "normal"
                    self.csta_endpoint.send(from_user=self.name, to_user=None, message="SystemStatusResponse")
                    user.update_outgoing_transactions(message)
                except IndexError:
                    break

    @staticmethod
    def snapshot_device(self):
        while True:
            self.events["SnapshotDevice"].wait()
            self.events["SnapshotDevice"].clear()
            while True:
                try:
                    snapshot_device_message = self.buffers["SnapshotDevice"].pop(0)
                    user = snapshot_device_message["snapshotObject"]
                    self.csta_endpoint.get_user(user).update_incoming_transactions(snapshot_device_message)
                    if not user:
                        debug("Invalid snapshot device user '{}'. Empty snapshotObject tag.".format(user))
                        return
                    self.send("SnapshotDeviceResponse", to_user=user)
                    self.csta_endpoint.get_user(user).update_outgoing_transactions(snapshot_device_message)
                except IndexError:
                    break
