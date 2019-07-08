"""
CircuitPython_MiniMQTT Module Tester

by Brent Rubell for Adafruit Industries, 2019
"""
import time
import board
import busio
from digitalio import DigitalInOut
import neopixel
from adafruit_esp32spi import adafruit_esp32spi
import adafruit_esp32spi.adafruit_esp32spi_socket as socket

from adafruit_minimqtt import MQTT

"""
Generic Unittest-like Assertions
"""
#pylint: disable=keyword-arg-before-vararg
def assertAlmostEqual(x, y, places=None, msg=''):
    """Raises an AssertionError if two float values are not equal.
    (from https://github.com/micropython/micropython-lib/blob/master/unittest/unittest.py)."""
    if x == y:
        return
    if places is None:
        places = 2
    if round(abs(y-x), places) == 0:
        return
    if not msg:
        msg = '%r != %r within %r places' % (x, y, places)
    assert False, msg

def assertRaises(exc,func=None, *args, **kwargs):
    """Raises based on exception context.
    (from https://github.com/micropython/micropython-lib/blob/master/unittest/unittest.py)"""
    try:
        func(*args, **kwargs)
        assert False, "%r not raised" % exc
    except Exception as e:
        if isinstance(e, exc):
            return
        raise

def assertIsNone(x):
    """Raises an AssertionError if x is None."""
    if x is None:
        raise AssertionError('%r is None'%x)

def assertEqual(val_1, val_2):
    """Raises an AssertionError if the two specified values are not equal."""
    if val_1 != val_2:
        raise AssertionError('Values are not equal:', val_1, val_2)

# MQTT Client Tests
def test_mqtt_create_client_esp32spi():
    """Creates an INSECURE MQTT client using an ESP32SPI socket connection."""
    # TODO: reflect test_mqtts_connect_disconnect_esp32spi
    mqtt_client = MQTT(socket, secrets['aio_url'], username=secrets['aio_user'], password=secrets['aio_password'],
                  esp = esp, is_ssl=False)
    assertEqual(mqtt_client.port, 1883)

def test_mqtts_connect_disconnect_esp32spi():
    """Creates a MQTTS client, connects, and attempts a disconnection."""
    mqtt_client = MQTT(socket, secrets['aio_url'], username=secrets['aio_user'], password=secrets['aio_password'],
                  esp = esp)
    assertEqual(mqtt_client.port, 8883)
    mqtt_client.connect()
    assertEqual(mqtt_client._is_connected, True)
    mqtt_client.disconnect()
    assertEqual(mqtt_client._is_connected, False)

def test_sub_pub():
    """Creates a MQTTS client, connects, subscribes, publishes, and checks data
    received from broker matches data sent by client"""
    MSG_TOPIC = 'brubell/feeds/testfeed'
    MSG_DATA  = 42
    mqtt_client = MQTT(socket, secrets['aio_url'], username=secrets['aio_user'], password=secrets['aio_password'],
                  esp = esp)
    # Callback responses
    callback_msgs = []
    def on_message(client, topic, msg):
        callback_msgs.append([topic, msg])
    mqtt_client.on_message = on_message
    mqtt_client.connect()
    assertEqual(mqtt_client._is_connected, True)
    mqtt_client.subscribe(MSG_TOPIC)
    mqtt_client.publish(MSG_TOPIC, MSG_DATA)
    start_timer = time.monotonic()
    print('listening...')
    while len(callback_msgs) == 0 and (time.monotonic() - start_timer < 30):
        mqtt_client.wait_for_msg()
    # check message and topic has been RX'd by the client's callback
    assertEqual(callback_msgs[0][0], MSG_TOPIC)
    assertEqual(callback_msgs[0][1], str(MSG_DATA))
    mqtt_client.disconnect()

def test_sub_pub_multiple():
    """Subscribe to multiple topics, publish to one, unsubscribe from both.
    """
    MSG_TOPIC_1 = 'brubell/feeds/testfeed1'
    MSG_TOPIC_2 = 'brubell/feeds/testfeed2'
    mqtt_client = MQTT(socket, secrets['aio_url'], username=secrets['aio_user'], password=secrets['aio_password'],
                  esp = esp)
    # Callback responses
    callback_msgs = []
    def on_message(client, topic, msg):
        callback_msgs.append([topic, msg])
    mqtt_client.on_message = on_message
    mqtt_client.connect()
    assertEqual(mqtt_client._is_connected, True)
    # subscribe to two topics with different QoS levels
    mqtt_client.subscribe([(MSG_TOPIC_1, 1), (MSG_TOPIC_2, 0)])
    mqtt_client.publish(MSG_TOPIC_2, 42)
    start_timer = time.monotonic()
    print('listening...')
    while len(callback_msgs) == 0 and (time.monotonic() - start_timer < 30):
        mqtt_client.wait_for_msg()
    # check message and topic has been RX'd by the client's callback
    assertEqual(callback_msgs[0][0], MSG_TOPIC_2)
    assertEqual(callback_msgs[0][1], str(42))
    mqtt_client.unsubscribe([MSG_TOPIC_1, MSG_TOPIC_2])
    mqtt_client.disconnect()

def test_publish_errors():
    """Testing invalid publish() calls, expecting specific MMQTExceptions.
    """
    MSG_TOPIC = 'brubell/feeds/testfeed'
    mqtt_client = MQTT(socket, secrets['aio_url'], username=secrets['aio_user'], password=secrets['aio_password'],
                  esp = esp)
    # Callback responses
    callback_msgs = []
    def on_message(client, topic, msg):
        callback_msgs.append([topic, msg])
    mqtt_client.on_message = on_message
    mqtt_client.connect()
    assertEqual(mqtt_client._is_connected, True)
    mqtt_client.subscribe(MSG_TOPIC)
    try:
        mqtt_client.publish(MSG_TOPIC, None)
    except MMQTException as exception:
        print(MMQTException)

# Timeout between tests, in seconds. This value depends on the MQTT broker.
TEST_TIMEOUT = 1

# Connection/Client Tests
conn_tests = [test_mqtt_create_client_esp32spi, test_mqtts_connect_disconnect_esp32spi]

# PUB/SUB API Tests
pub_sub_tests = [test_sub_pub, test_sub_pub_multiple, test_publish_errors]

# The test routine runs the following test(s):
tests = pub_sub_tests

# Get wifi details and more from a secrets.py file
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# Define an ESP32SPI network interface
esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)
status_light = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.2) # Uncomment for Most Boards


# Establish ESP32SPI connection
print("Connecting to AP...")
while not esp.is_connected:
    try:
        esp.connect_AP(secrets['ssid'], secrets['password'])
    except RuntimeError as e:
        print("could not connect to AP, retrying: ",e)
        continue
print("Connected to", str(esp.ssid, 'utf-8'), "\tRSSI:", esp.rssi)

## Test Routine ##
start_time = time.monotonic()
for i in enumerate(tests):
    print('Running test: ', i)
    i[1]()
    print('OK!')
    time.sleep(TEST_TIMEOUT)
print('Ran {0} tests in {1}s.'.format(len(tests), time.monotonic() - start_time))
