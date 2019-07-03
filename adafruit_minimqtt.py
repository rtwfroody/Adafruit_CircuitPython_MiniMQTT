# The MIT License (MIT)
#
# Copyright (c) 2019 Brent Rubell for Adafruit Industries
#
# Original Work Copyright (c) 2016 Paul Sokolovsky, uMQTT
# Modified Work Copyright (c) 2019 Bradley Beach, esp32spi_mqtt
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""
`adafruit_minimqtt`
================================================================================

MQTT Library for CircuitPython.

* Author(s): Brent Rubell

Implementation Notes
--------------------

**Software and Dependencies:**

* Adafruit CircuitPython firmware for the supported boards:
  https://github.com/adafruit/circuitpython/releases

"""
import time
import struct
from micropython import const
from random import randint
import microcontroller

__version__ = "0.0.0-auto.0"
__repo__ = "https://github.com/adafruit/Adafruit_CircuitPython_MiniMQTT.git"


# Client-specific variables
MQTT_MSG_MAX_SZ = const(268435455)
MQTT_MSG_SZ_LIM = const(10000000)
MQTT_TOPIC_SZ_LIMIT = const(65536)
MQTT_TCP_PORT = const(1883)

# MQTT Commands
MQTT_PING_REQ = b'\xc0'
MQTT_PINGRESP = b'\xd0'
MQTT_SUB = bytearray(b'\x82\0\0\0')
MQTT_PUB = bytearray(b'\x30\0')
MQTT_CON = bytearray(b'\x10\0\0')
# Variable header [MQTT 3.1.2]
MQTT_CON_HEADER = bytearray(b"\x04MQTT\x04\x02\0\0")
MQTT_DISCONNECT = b'\xe0\0'

CONNACK_ERRORS = {const(0x01) : 'Connection Refused - Incorrect Protocol Version',
                   const(0x02) : 'Connection Refused - ID Rejected',
                   const(0x03) : 'Connection Refused - Server unavailable',
                   const(0x04) : 'Connection Refused - Incorrect username/password',
                   const(0x05) : 'Connection Refused - Unauthorized'}

class MMQTTException(Exception):
    pass

class MQTT:
    """
    MQTT client interface for CircuitPython devices.
    :param esp: ESP32SPI object.
    :param socket: ESP32SPI Socket object.
    :param str server_address: Server URL or IP Address.
    :param int port: Optional port definition, defaults to 8883.
    :param str username: Username for broker authentication.
    :param str password: Password for broker authentication.
    :param str client_id: Optional client identifier, defaults to a randomly generated id.
    :param bool is_ssl: Enables TCP mode if false (port 1883). Defaults to True (port 8883).
    """
    TCP_MODE = const(0)
    TLS_MODE = const(2)
    def __init__(self, esp, socket, server_address, port=8883, username=None,
                    password = None, client_id=None, is_ssl=True):
        if esp and socket is not None:
            self._esp = esp
            self._socket = socket
        else:
            raise NotImplementedError('MiniMQTT currently only supports an ESP32SPI connection.')
        self.port = port
        if not is_ssl:
            self.port = MQTT_TCP_PORT
        self._user = username
        self._pass = password
        if client_id is not None:
            # user-defined client_id MAY allow client_id's > 23 bytes or
            # non-alpha-numeric characters
            self._client_id = client_id
        else:
            # assign a unique client_id
            self._client_id = 'cpy{0}{1}'.format(microcontroller.cpu.uid[randint(0, 15)], randint(0, 9))
            # generated client_id's enforce length rules
            if len(self._client_id) > 23 or len(self._client_id) < 1:
                raise ValueError('MQTT Client ID must be between 1 and 23 bytes')
        # subscription method handler dictionary
        self._method_handlers = {}
        self._is_connected = False
        self._msg_size_lim = MQTT_MSG_SZ_LIM
        self.server = server_address
        self.packet_id = 0
        self._keep_alive = 0
        self._pid = 0
        # paho-style method callbacks
        self._on_connect = None
        self._on_disconnect = None
        self._on_publish = None
        self._on_subscribe = None
        self._on_log = None
        self._user_data = None
        self._is_loop = False
        self.last_will()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.deinit()

    def deinit(self):
        """Disconnects the MQTT client from the broker.
        """
        self.disconnect()

    def last_will(self, topic=None, message=None, qos=0, retain=False):
        """Sets the last will and testament properties. MUST be called before connect().
        :param str topic: MQTT Broker topic.
        :param str message: Last will disconnection message.
        :param int qos: Quality of Service level.
        :param bool retain: Specifies if the message is to be retained when it is published. 
        """
        if self._is_connected:
            raise MMQTTException('Last Will should be defined before connect() is called.')
        if qos < 0 or qos > 2:
            raise MMQTTException("Invalid QoS level,  must be between 0 and 2.")
        self._lw_qos = qos
        self._lw_topic = topic
        self._lw_msg = message
        self._lw_retain = retain

    def reconnect(self, retries=30, resub_topics=False):
        """Attempts to reconnect to the MQTT broker.
        :param int retries: Amount of retries before resetting the ESP32 hardware.
        :param bool resub_topics: Client resubscribes to previously subscribed topics upon
            a successful reconnection.
        """
        retries = 0
        while not self._is_connected:
            try:
                self.connect(False)
            except OSError as e:
                print('Failed to connect to the broker, retrying\n', e)
                retries+=1
                if retries >= 30:
                    retries = 0
                    self._esp.reset()
                continue
            self._is_connected = True
            if resub_topics:
                while len(self._method_handlers) > 0:
                    feed = self._method_handlers.popitem()
                    self.subscribe(feed)

    def is_connected(self):
        """Returns MQTT client session status."""
        return self._is_connected

    ### Core MQTT Methods ###

    def connect(self, clean_session=True):
        """Initiates connection with the MQTT Broker.
        :param bool clean_session: Establishes a persistent session
            with the broker. Defaults to a non-persistent session.
        """
        if self._esp:
            self._socket.set_interface(self._esp)
            self._sock = self._socket.socket()
        else:
            raise TypeError('ESP32SPI interface required!')
        self._sock.settimeout(10)
        if self.port == 8883:
            try:
                self._sock.connect((self.server, self.port), TLS_MODE)
            except RuntimeError:
                raise MMQTTException("Invalid server address defined.")
        else:
            addr = self._socket.getaddrinfo(self.server, self.port)[0][-1]
            try:
                self._sock.connect(addr, TCP_MODE)
            except RuntimeError:
                raise MMQTTException("Invalid server address defined.")
        premsg = MQTT_CON
        msg = MQTT_CON_HEADER
        msg[6] = clean_session << 1
        sz = 12 + len(self._client_id)
        if self._user is not None:
            sz += 2 + len(self._user) + 2 + len(self._pass)
            msg[6] |= 0xC0
        if self._keep_alive:
            assert self._keep_alive < MQTT_TOPIC_SZ_LIMIT
            msg[7] |= self._keep_alive >> 8
            msg[8] |= self._keep_alive & 0x00FF
        if self._lw_topic:
            sz += 2 + len(self._lw_topic) + 2 + len(self._lw_msg)
            msg[6] |= 0x4 | (self._lw_qos & 0x1) << 3 | (self._lw_qos & 0x2) << 3
            msg[6] |= self._lw_retain << 5
        i = 1
        while sz > 0x7f:
            premsg[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz
        self._sock.write(premsg)
        self._sock.write(msg)
        # [MQTT-3.1.3-4]
        self._send_str(self._client_id)
        if self._lw_topic:
            # [MQTT-3.1.3-11]
            self._send_str(self._lw_topic)
            self._send_str(self._lw_msg)
        if self._user is None:
            self._user = None
        else:
            self._send_str(self._user)
            self._send_str(self._pass)
        rc = self._sock.read(4)
        assert rc[0] == const(0x20) and rc[1] == const(0x02)
        if rc[3] !=0:
            raise MMQTTException(CONNACK_ERRORS[rc[3]])
        self._is_connected = True
        result = rc[2] & 1
        if self._on_connect is not None:
            self._on_connect(self, self._user_data, result, rc[3]) 
        return result

    def disconnect(self):
        """Disconnects from the broker.
        """
        if self._sock is None:
            raise MMQTTException("MiniMQTT is not connected.")
        self._sock.write(MQTT_DISCONNECT)
        self._sock.close()
        self._is_connected = False
        if self._on_disconnect is not None:
            self._on_disconnect(self, self._user_data, 0)

    def ping(self):
        """Pings the MQTT Broker to confirm if the server is alive or
        if the network connection is active.
        Raises an error if server is not alive.
        Returns PINGRESP if server is alive. 
        """
        # note: sock.write handles the PINGRESP
        self._sock.write(MQTT_PING_REQ)
        res = self._sock.read(1)
        if res != MQTT_PINGRESP:
            raise MMQTTException('PINGRESP was not received')
        return res



    def publish(self, topic, msg, retain=False, qos=0):
        """Publishes a message to the MQTT broker.
        :param str topic: Unique topic identifier.
        :param str msg: Data to send to the broker.
        :param bool retain: Whether the message is saved by the broker.
        :param int qos: Quality of Service level for the message.
        """
        if topic is None or len(topic) == 0:
            raise MMQTTException("Invalid MQTT Topic, must have length > 0.")
        if '+' in topic or '#' in topic:
            raise MMQTTException('Topic can not contain wildcards.')
        # check msg/qos kwargs
        if msg is None:
            raise MMQTTException('Message can not be None.')
        elif isinstance(msg, (int, float)):
            msg = str(msg).encode('ascii')
        elif isinstance(msg, str):
            msg = str(msg).encode('utf-8')
        else:
            raise MMQTTException('Invalid message data type.')
        if len(msg) > MQTT_MSG_MAX_SZ:
            raise MMQTTException('Message size larger than %db.'%MQTT_MSG_MAX_SZ)
        if qos < 0 or qos > 2:
            raise MMQTTException("Invalid QoS level,  must be between 0 and 2.")
        if self._sock is None:
            raise MMQTTException("MiniMQTT not connected.")
        pkt = MQTT_PUB
        pkt[0] |= qos << 1 | retain
        sz = 2 + len(topic) + len(msg)
        if qos > 0:
            sz += 2
        assert sz < 2097152
        i = 1
        while sz > 0x7f:
            pkt[i] = (sz & 0x7f) | const(0x80)
            sz >>= 7
            i += 1
        pkt[i] = sz
        self._sock.write(pkt)
        self._send_str(topic)
        if qos == 0:
            if self._on_publish is not None:
                self._on_publish(self, self._user_data, self._pid)
        if qos > 0:
            self.pid += 1
            pid = self.pid
            struct.pack_into("!H", pkt, 0, pid)
            self._sock.write(pkt)
            if self._on_publish is not None:
                self._on_publish(self, self._user_data, pid)
        self._sock.write(msg)
        if qos == 1:
            while 1:
                op = self.wait_for_msg()
                if op == const(0x40):
                    sz = self._sock.read(1)
                    assert sz == b"\x02"
                    rcv_pid = self._sock.read(2)
                    rcv_pid = rcv_pid[0] << 8 | rcv_pid[1]
                    if self._on_publish is not None:
                        self._on_publish(self, self._user_data, rcv_pid)
                    if pid == rcv_pid:
                        return
        elif qos == 2:
            if self._on_publish is not None:
                raise NotImplementedError('on_publish callback not implemented for QoS > 1.')
            assert 0

    def subscribe(self, topic, method_handler=None, qos=0):
        """Subscribes to a topic on the MQTT Broker.
        This method can subscribe to one topics or multiple topics.
        :param str topic: Unique topic identifier.
        :param method method_handler: Predefined method for handling messages
            recieved from a topic. Defaults to default_sub_handler if None.
        :param int qos: Quality of Service level for the topic.

        Example of subscribing to one topic:
        .. code-block:: python
            mqtt_client.subscribe('topics/ledState')

        Example of subscribing to one topic and setting the Quality of Service level to 1:
        .. code-block:: python
            mqtt_client.subscribe('topics/ledState', 1)
        
        Example of subscribing to one topic and attaching a method handler:
        .. code-block:: python
            mqtt_client.subscribe('topics/ledState', led_setter)
        """
        if qos < 0 or qos > 2:
            raise MMQTTException('QoS level must be between 1 and 2.')
        if topic is None or len(topic) == 0:
            raise MMQTTException("Invalid MQTT Topic, must have length > 0.")
        if topic in self._method_handlers:
            raise MMQTTException('Already subscribed to topic.')
        # associate topic subscription with method_handler.
        if method_handler is None:
            self._method_handlers.update( {topic : self.default_sub_handler} )
        else:
            self._method_handlers.update( {topic : custom_method_handler} )
        if self._sock is None:
            raise MMQTTException("MiniMQTT not connected.")
        pkt = MQTT_SUB
        self._pid += 11
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, self._pid)
        self._sock.write(pkt)
        # [MQTT-3.8.3-1]
        self._send_str(topic)
        self._sock.write(qos.to_bytes(1, "little"))
        while 1:
            op = self.wait_for_msg()
            if op == 0x90:
                rc = self._sock.read(4)
                assert rc[1] == pkt[2] and rc[2] == pkt[3]
                if rc[3] == 0x80:
                    raise MMQTTException('SUBACK Failure!')
                if self._on_subscribe is not None:
                    self.on_subscribe(self, self._user_data, rc[3])
                return

    def unsubscribe(self, topic, qos=0):
        """Unsubscribes from a MQTT topic.
        """
        pkt = bytearray(b'\xA0\0\0\0')
        self._pid += 11
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, self._pid)
        print(pkt)
        self._sock.write(pkt)
        while 1:
            print('waiting for response...')
            op = self.wait_for_msg()
            print('op,', op)

    @property
    def mqtt_msg(self):
        """Returns maximum MQTT payload and topic size."""
        return self._msg_size_lim, MQTT_TOPIC_SZ_LIMIT

    @mqtt_msg.setter
    def mqtt_msg(self, msg_size):
        """Sets the maximum MQTT message payload size.
        :param int msg_size: Maximum MQTT payload size.
        """
        if msg_size < MQTT_MSG_MAX_SZ:
            self.__msg_size_lim = msg_size

    def publish_multiple(self, data, timeout=1.0):
        """Publishes to multiple MQTT broker topics.
        :param tuple data: A list of tuple format:
            :param str topic: Unique topic identifier.
            :param str msg: Data to send to the broker.
            :param bool retain: Whether the message is saved by the broker.
            :param int qos: Quality of Service level for the message.
        :param float timeout: Timeout between calls to publish(). This value
            is usually set by your MQTT broker. Defaults to 1.0
        """
        # TODO: Untested!
        for i in range(len(data)):
            topic = data[i][0]
            msg = data[i][1]
            try:
                if data[i][2]:
                    retain = data[i][2]
            except IndexError:
                retain = False
                pass
            try:
                if data[i][3]:
                    qos = data[i][3]
            except IndexError:
                qos = 0
                pass
            self.publish(topic, msg, retain, qos)
            time.sleep(timeout)

    def subscribe_multiple(self, topic_info, timeout=1.0):
        """Subscribes to multiple MQTT broker topics.
        :param tuple topic_info: A list of tuple format:
            :param str topic: Unique topic identifier.
            :param method method_handler: Predefined method for handling messages
                recieved from a topic. Defaults to default_sub_handler if None.
            :param int qos: Quality of Service level for the topic. Defaults to 0.
        :param float timeout: Timeout between calls to subscribe().
        """
        #TODO: This could be simplified
        # 1 mqtt subscription call, multiple topics
        print('topics:', topic_info)
        for i in range(len(topic_info)):
            topic = topic_info[i][0]
            try:
                if topic_info[i][1]:
                    method_handler = topic_info[i][1]
            except IndexError:
                method_handler = None
                pass
            try:
                if topic_info[i][2]:
                    qos = topic_info[i][2]
            except IndexError:
                qos = 0
                pass
            print('Subscribing to:', topic, method_handler, qos)
            self.subscribe(topic, method_handler, qos)
            time.sleep(timeout)

    def wait_for_msg(self, timeout=0.0):
        """Waits for and processes network events. Returns if successful.
        :param bool blocking: Set the blocking or non-blocking mode of the socket.
        :param float timeout: The time in seconds to wait for network traffic before returning.
        """
        self._sock.settimeout(timeout)
        res = self._sock.read(1)
        if res in [None, b""]:
            return None
        if res == MQTT_PINGRESP:
            sz = self._sock.read(1)[0]
            assert sz == 0
            return None
        op = res[0]
        if op & 0xf0 != 0x30:
            return op
        sz = self._recv_len()
        topic_len = self._sock.read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = self._sock.read(topic_len)
        topic = str(topic, 'utf-8')
        sz -= topic_len + 2
        if op & 6:
            pid = self._sock.read(2)
            pid = pid[0] << 8 | pid[1]
            sz -= 2
        msg = self._sock.read(sz)
        # call the topic's handler method
        if topic in self._method_handlers:
            method_handler = self._method_handlers[topic]
            method_handler(topic, str(msg, 'utf-8'))
        if op & 6 == 2:
            pkt = bytearray(b"\x40\x02\0\0")
            struct.pack_into("!H", pkt, 2, pid)
            self._sock.write(pkt)
        elif op & 6 == 4:
            assert 0
        return op

    def _recv_len(self):
        """Receives the size of the topic length."""
        n = 0
        sh = 0
        while 1:
            b = self._sock.read(1)[0]
            n |= (b & 0x7f) << sh
            if not b & 0x80:
                return n
            sh += 7

    def default_sub_handler(self, topic, msg):
        """Default feed subscription handler method.
        :param str topic: Subscription topic.
        :param str msg: Message content.
        """
        print('New message on {0}: {1}'.format(topic, msg))

    def _send_str(self, string):
        """Packs a string into a struct, and writes it to a socket as an utf-8 encoded string.
        :param str string: String to write to the socket.
        """
        self._sock.write(struct.pack("!H", len(string)))
        if type(string) == str:
            self._sock.write(str.encode(string, 'utf-8'))
        else:
            self._sock.write(string)

    # Network Loop Methods

    def loop(self, timeout=1.0):
        """Call regularly to process network events.
        This function blocks for up to timeout seconds. 
        Timeout must not exceed the keepalive value for the client or
        your client will be regularly disconnected by the broker.
        :param float timeout: Blocks between calls to wait_for_msg()
        """
        # TODO: Untested!
        self.wait_for_msg(timeout)
    
    def loop_forever(self):
        """Blocking network loop, will not return until disconnect() is called from
        the client. Automatically handles the re-connection.
        """
        # TODO!
        return None

    ## Logging ##
    # TODO: Set up Logging with the CircuitPython logger module.

    ## Acknowledgement Callbacks ##

    @property
    def user_data(self):
        """Returns the user_data variable passed to callbacks.
        """
        return self._user_data
    
    @user_data.setter
    def user_data(self, data):
        """Sets the private user_data variable passed to callbacks.
        :param data: Any data type.
        """
        self._user_data = data

    @property
    def on_connect(self):
        """Called when the MQTT broker responds to a connection request.
        """
        return self._on_connect
    
    @on_connect.setter
    def on_connect(self, method):
        """Defines the method which runs when the client is connected.
        :param unbound_method method: user-defined method for connection.

        The on_connect method signature takes the following format:
            on_connect_method(client, userdata, flags, rc)
        and expects the following parameters:
        :param client: MiniMQTT Client Instance.
        :param userdata: User data, previously set in the user_data method.
        :param flags: CONNACK flags.
        :param int rc: Response code.
        """
        self._on_connect = method
    
    @property
    def on_disconnect(self):
        """Called when the MQTT broker responds to a disconnection request.
        """
        return self._on_disconnect
    
    @on_disconnect.setter
    def on_disconnect(self, method):
        """Defines the method which runs when the client is disconnected.
        :param unbound_method method: user-defined method for disconnection.
        
        The on_disconnect method signature takes the following format:
            on_disconnect_method(client, userdata, rc)
        and expects the following parameters:
        :param client: MiniMQTT Client Instance.
        :param userdata: User data, previously set in the user_data method.
        :param int rc: Response code.
        """
        self._on_disconnect = method

    @property
    def on_publish(self):
        """Called when the MQTT broker responds to a publish request.
        """
        return self._on_publish
    
    @on_publish.setter
    def on_publish(self, method):
        """Defines the method which runs when the client publishes data to a feed.
        :param unbound_method method: user-defined method for disconnection.
        
        The on_publish method signature takes the following format:
            on_publish(client, userdata, rc)
        and expects the following parameters:
        :param client: MiniMQTT Client Instance.
        :param userdata: User data, previously set in the user_data method.
        :param int rc: Response code.
        """
        self._on_publish = method

    @property
    def on_subscribe(self):
        """Called when the MQTT broker successfully subscribes to a feed.
        """
        return self._on_subscribe

    @on_subscribe.setter
    def on_subscribe(self, method):
        """Defines the method which runs when a client subscribes to a feed.

        :param unbound_method method: user-defined method for disconnection.
        
        The on_subscribe method signature takes the following format:
            on_subscribe(client, userdata, rc)
        and expects the following parameters:
        :param client: MiniMQTT Client Instance.
        :param userdata: User data, previously set in the user_data method.
        :param int granted_qos: QoS level the broker has granted the subscription request..
        """
        self._on_subscribe = method

    @property
    def on_publish(self):
        """Called when the MQTT broker successfully publishes to a feed.
        """
        return self._on_publish

    @on_publish.setter
    def on_publish(self, method):
        """Defines the method which runs when a client publishes to a feed.

        :param unbound_method method: user-defined method for disconnection.
        
        The on_publish method signature takes the following format:
            on_publish(client, userdata, rc)
        and expects the following parameters:
        :param client: MiniMQTT Client Instance.
        :param userdata: User data, previously set in the user_data method.
        :param int granted_qos: QoS level the broker has granted the subscription request..
        """
        self._on_publish = method

    # TODO: Implement on_log