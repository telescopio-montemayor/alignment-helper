#!/usr/bin/env python3

import time
import logging
import queue
import argparse
import threading
from collections import defaultdict

from astropy.time import Time
import PyIndi
from socketIO_client import SocketIO, BaseNamespace

# Vega
DEFAULT_RA  = (279.23473479 * 24.0)/360.0
DEFAULT_DEC = +38.78368896

ENCODER_SERVER_HOST = 'localhost'
ENCODER_SERVER_PORT = '5000'
RA_ENCODER_NAME = 'Encoder_RA'
DEC_ENCODER_NAME = 'Encoder_DEC'

SITE_LONGITUDE=-58.381592

log = logging.getLogger('ethernet-encoder-sync')
# The indi wrapper doesn't like when we use it from a thread other than the
# main one, so we use a queue as a simple notification mechanism
channel = queue.Queue()

# From libindi/libs/indicom.c


def range24(v):
    res = v
    while res < 0:
        res += 24.0
    while res > 24:
        res -= 24.0

    return res


def rangeDec(decdegrees):
    if ((decdegrees >= 270.0) and (decdegrees <= 360.0)):
        return (decdegrees - 360.0)
    if ((decdegrees >= 180.0) and (decdegrees < 270.0)):
        return (180.0 - decdegrees)
    if ((decdegrees >= 90.0) and (decdegrees < 180.0)):
        return (180.0 - decdegrees)

    return decdegrees


def LST(longitude=None):
    if longitude is None:
        longitude = SITE_LONGITUDE
    print ('LONG: ', longitude)
    now = Time.now()
    # XXX FIXME: traer longitud de gps o parametro
    lst = now.sidereal_time('apparent', longitude=longitude).to_value()
    return range24(lst)
# First call to sidereal_time() takes a while to initialize
LST()


def calc_ra(deg):
    ra = deg / 15.0
    ra += LST()
    return range24(ra)


def calc_dec(deg):
    return rangeDec(deg)


class EncoderClient():
    def __init__(self, host=ENCODER_SERVER_HOST, port=ENCODER_SERVER_PORT, ra_name=RA_ENCODER_NAME, dec_name=DEC_ENCODER_NAME):
        self.host = host
        self.port = port
        self.ra_name = ra_name
        self.dec_name = dec_name

        self.connected = False
        self.firstPosition = True
        self.ra = None
        self.dec = None
        # name -> [function]
        self.__callbacks = defaultdict(list)

        socketIO = SocketIO(host, port, BaseNamespace)
        socketIO.on('position', self.__on_position)
        socketIO.on('connect', self.__on_connect)
        socketIO.on('reconnect', self.__on_connect)
        socketIO.on('disconnect', self.__on_disconnect)

        self.listener_thread = threading.Thread(target=socketIO.wait)
        self.listener_thread.daemon = True
        self.listener_thread.start()

    def __call_callbacks(self, name):
        to_remove = []
        for callback in self.__callbacks[name]:
            ret = callback(self)
            if not ret:
                to_remove.append(callback)
        for callback in to_remove:
            self.__callbacks[name].remove(callback)

    def __on_connect(self):
        self.connected = True
        self.__call_callbacks('connect')

    def __on_disconnect(self):
        self.connected = False
        self.firstPosition = True
        self.ra = None
        self.dec = None
        self.__call_callbacks('disconnect')

    def __on_position(self, data):
        # hackish workaround
        if not self.connected:
            self.__on_connect()

        if data['name'] == self.ra_name:
            self.ra = calc_ra(data['position_deg'])
        if data['name'] == self.dec_name:
            self.dec = calc_dec(data['position_deg'])

        if self.has_position():
            self.__call_callbacks('position')
            if self.firstPosition:
                self.firstPosition = False
                self.__call_callbacks('first-position')

    def on(self, name, callback):
        self.__callbacks[name].append(callback)

    def has_position(self):
        return self.connected and (self.ra is not None) and (self.dec is not None)


class SyncClient(PyIndi.BaseClient):
    def __init__(self, device_name='Telescope Simulator'):
        super(SyncClient, self).__init__()
        self.__is_connected = False
        self.__device_connected = False
        self.device = None
        self.device_name = device_name

    def start(self):
        self.watchDevice(self.device_name)
        while not self.connectServer():
            time.sleep(0.5)

    def sync(self, ra=DEFAULT_RA, dec=DEFAULT_DEC):
        device = self.device
        if not device:
            return

        log.info('Syncing to {} {}'.format(ra, dec))

        while not(device.isConnected()):
            time.sleep(0.5)
        self.__device_connected = True

        on_coord_set = device.getSwitch("ON_COORD_SET")
        while not on_coord_set:
            time.sleep(0.5)
            on_coord_set = device.getSwitch("ON_COORD_SET")

        # the order below is defined in the property vector, look at the standard Properties page
        original_coord_set = [x.s for x in on_coord_set]
        on_coord_set[0].s = PyIndi.ISS_OFF  # TRACK
        on_coord_set[1].s = PyIndi.ISS_OFF  # SLEW
        on_coord_set[2].s = PyIndi.ISS_ON   # SYNC
        self.sendNewSwitch(on_coord_set)

        radec = device.getNumber("EQUATORIAL_EOD_COORD")
        while not radec:
            time.sleep(0.5)
            radec = device.getNumber("EQUATORIAL_EOD_COORD")

        radec[0].value = ra
        radec[1].value = dec
        self.sendNewNumber(radec)

        while (radec.s == PyIndi.IPS_BUSY):
            time.sleep(0.5)

        for idx, value in enumerate(original_coord_set):
            on_coord_set[idx].s = value
        self.sendNewSwitch(on_coord_set)

    def newDevice(self, device):
        name = device.getDeviceName()
        if name == self.device_name:
            log.info('Device found: {}'.format(name))
            self.device = device
            self.__device_connected = device.isConnected()
            channel.put(device)

    def newProperty(self, p):
        pass

    def removeProperty(self, p):
        pass

    def newBLOB(self, bp):
        pass

    def newSwitch(self, svp):
        pass

    def newNumber(self, nvp):
        pass

    def newText(self, tvp):
        pass

    def newLight(self, lvp):
        pass

    def newMessage(self, d, m):
        device = self.device
        if device:
            old_status = self.__device_connected
            self.__device_connected = device.isConnected()
            if (not old_status) and self.__device_connected:
                log.info('Device connected')
                channel.put(device)

    def serverConnected(self):
        log.info('Connected to INDI Server')
        self.__is_connected = True

    def serverDisconnected(self, code):
        log.info('Connection to INDI Server closed')
        self.__is_connected = False
        self.__device_connected = False
        self.device = None
        self.start()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--host',
                        required=False,
                        type=str,
                        default='localhost',
                        help='INDI host to connect to, defaults to localhost')

    parser.add_argument('--port',
                        required=False,
                        type=int,
                        default=7624,
                        help='INDI port to connect to, defaults to 7624')

    parser.add_argument('--site-longitude',
                        required=False,
                        type=float,
                        default=SITE_LONGITUDE,
                        help='Site longitude in degrees')

    parser.add_argument('--encoder-server-host',
                        required=False,
                        type=str,
                        default='localhost',
                        help='Ethernet encoder server host, defaults to localhost')

    parser.add_argument('--encoder-server-port',
                        required=False,
                        type=int,
                        default=5000,
                        help='Ethernet encoder server port, defaults to 5000')

    parser.add_argument('--ra-encoder-name',
                        required=False,
                        type=str,
                        default=RA_ENCODER_NAME,
                        help='RA Encoder name from server')

    parser.add_argument('--dec-encoder-name',
                        required=False,
                        type=str,
                        default=DEC_ENCODER_NAME,
                        help='DEC Encoder name from server')

    parser.add_argument('--device',
                        required=False,
                        type=str,
                        default='Telescope Simulator',
                        help='Telescope to sync, defaults to "Telescope Simulator"')

    parser.add_argument('--debug',
                        required=False,
                        action='store_true',
                        help='Shows debug messages')

    args = parser.parse_args()

    # Not pretty but works
    SITE_LONGITUDE = args.site_longitude

    logging.getLogger('socketIO-client').setLevel(logging.ERROR)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig()

    indiclient = SyncClient(device_name=args.device)
    indiclient.setServer(args.host, args.port)
    indiclient.start()

    encoderclient = EncoderClient(host=args.encoder_server_host, port=args.encoder_server_port, ra_name=args.ra_encoder_name, dec_name=args.dec_encoder_name)

    def on_first_position(ec):
        indiclient.sync(encoderclient.ra, encoderclient.dec)
        return True
    encoderclient.on('first-position', on_first_position)

    while True:
        device = channel.get()
        if encoderclient.has_position():
            indiclient.sync(encoderclient.ra, encoderclient.dec)
