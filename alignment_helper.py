#!/usr/bin/env python3

import logging
import queue
import argparse
from collections import defaultdict

from flask import Flask, render_template
from flask.json import jsonify
from flask_socketio import SocketIO


from astropy.time import Time
import PyIndi


app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app)


@app.route('/')
def index():
    return render_template('index.html')


SITE_LONGITUDE=-58.381592

# Vega
DEFAULT_RA  = (279.23473479 * 24.0)/360.0
DEFAULT_DEC = +38.78368896

log = logging.getLogger('alignment-helper')
# The indi wrapper doesn't like when we use it from a thread other than the
# main one, so we use a queue as a simple notification mechanism
indi_channel = queue.Queue()
channel = queue.Queue()
coords_channel = queue.Queue()

SCOPE_STATUS = {
    'RA': 0,
    'DEC': 0,
    'connected': False,
}

INDI_STATUS = {
    'connected': False
}

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
    now = Time.now()
    # XXX FIXME: traer longitud de gps o parametro
    lst = now.sidereal_time('apparent', longitude=longitude).to_value()
    return range24(lst)
# First call to sidereal_time() takes a while to initialize
LST()   # noqa


def calc_ra(deg):
    ra = deg / 15.0
    ra += LST()
    return range24(ra)


def calc_dec(deg):
    return rangeDec(deg)


class SyncClient(PyIndi.BaseClient):
    def __init__(self, device_name='Telescope Simulator'):
        super(SyncClient, self).__init__()
        self.__is_connected = False
        self.__device_connected = False
        self.device = None
        self.device_name = device_name
        self.watchDevice(self.device_name)

    def start(self):
        while not self.connectServer():
            socketio.sleep(0.5)

    def __move(self, ra=DEFAULT_RA, dec=DEFAULT_DEC, on_set='sync'):
        device = self.device
        if not device:
            return

        log.info('Moving to {} {} ({})'.format(ra, dec, on_set))

        while not(device.isConnected()):
            socketio.sleep(0.1)
        self.__device_connected = True

        on_coord_set = device.getSwitch("ON_COORD_SET")
        while not on_coord_set:
            socketio.sleep(0.1)
            on_coord_set = device.getSwitch("ON_COORD_SET")

        # the order below is defined in the property vector, look at the standard Properties page
        original_coord_set = [x.s for x in on_coord_set]
        on_coord_set[0].s = PyIndi.ISS_OFF  # TRACK
        on_coord_set[1].s = PyIndi.ISS_OFF  # SLEW
        on_coord_set[2].s = PyIndi.ISS_OFF  # SYNC

        switch_idx = {
            'track': 0,
            'slew':  1,
            'sync':  2,
        }.get(on_set, 1)
        on_coord_set[switch_idx].s = PyIndi.ISS_ON
        self.sendNewSwitch(on_coord_set)

        radec = device.getNumber("EQUATORIAL_EOD_COORD")
        while not radec:
            socketio.sleep(0.1)
            radec = device.getNumber("EQUATORIAL_EOD_COORD")

        radec[0].value = ra
        radec[1].value = dec
        self.sendNewNumber(radec)

        while (radec.s == PyIndi.IPS_BUSY):
            socketio.sleep(0.1)

        for idx, value in enumerate(original_coord_set):
            on_coord_set[idx].s = value
        self.sendNewSwitch(on_coord_set)

    def sync(self, ra=DEFAULT_RA, dec=DEFAULT_DEC):
        return self.__move(ra, dec, 'sync')

    def go_to(self, ra, dec, on_set='track'):
        return self.__move(ra, dec, on_set)

    def set_tracking(self, tracking=True):

        device = self.device
        if not device:
            return

        while not(device.isConnected()):
            socketio.sleep(0.1)
        self.__device_connected = True

        track_state = device.getSwitch("TELESCOPE_TRACK_STATE")
        while not track_state:
            socketio.sleep(0.1)
            track_state = device.getSwitch("TELESCOPE_TRACK_STATE")

        if tracking:
            track_state[0].s = PyIndi.ISS_ON
            track_state[1].s = PyIndi.ISS_OFF
        else:
            track_state[0].s = PyIndi.ISS_OFF
            track_state[1].s = PyIndi.ISS_ON

        self.sendNewSwitch(track_state)

    def get_coords(self):
        device = self.device
        if not device:
            return None

        while not(device.isConnected()):
            socketio.sleep(0.1)
        self.__device_connected = True

        radec = device.getNumber("EQUATORIAL_EOD_COORD")
        while not radec:
            socketio.sleep(0.1)
            radec = device.getNumber("EQUATORIAL_EOD_COORD")

        return {
            'RA': radec[0].value,
            'DEC': radec[1].value,
        }

    def removeDevice(self, device):
        name = device.getDeviceName()
        if name == self.device_name:
            log.info('Device removed: {}'.format(name))
            self.device = None
            self.__device_connected = False
            indi_channel.put(False)
            channel.put(device)

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
        if nvp.name == 'EQUATORIAL_EOD_COORD':
            coords_channel.put({
                'RA': nvp[0].value,
                'DEC': nvp[1].value,
            })

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

            if old_status and (not self.__device_connected):
                log.info('Device DISCONNECTED')
                print('Device DISCONNECTED')
                channel.put(device)


    def serverConnected(self):
        log.info('Connected to INDI Server')
        if not self.__is_connected:
            self.__is_connected = True
            indi_channel.put(self.__is_connected)

    def serverDisconnected(self, code):
        log.info('Connection to INDI Server closed')
        self.__is_connected = False
        self.device = None
        if self.__is_connected:
            self.__is_connected = False
            indi_channel.put(self.__is_connected)
        self.start()


def coords_send_task(*args, **kwargs):
    while True:
        try:
            coords = coords_channel.get_nowait()
            socketio.emit('EQUATORIAL_EOD_COORD', coords, broadcast=True)
            SCOPE_STATUS['RA'] = coords['RA']
            SCOPE_STATUS['DEC'] = coords['DEC']
        except queue.Empty:
            pass
        finally:
            socketio.sleep(.1)


def indi_monitor_task(*args, **kwargs):
    while True:
        try:
            connected = indi_channel.get_nowait()
            INDI_STATUS['connected'] = connected
            socketio.emit('INDI_STATUS', INDI_STATUS, broadcast=True)
        except queue.Empty:
            pass
        finally:
            socketio.sleep(.1)


def build_device_monitor_task(indiclient):
    def device_monitor_task(*args, **kwargs):
        while True:
            try:
                device = channel.get_nowait()
                device_name = device.getDeviceName()
                connected = device.isConnected()

                SCOPE_STATUS['connected'] = connected
                if connected:
                    print('CONN')
                    socketio.emit('DEVICE_CONNECTED', device_name, broadcast=True)
                    coords = indiclient.get_coords()
                    if coords:
                        coords_channel.put(coords)
                else:
                    socketio.emit('DEVICE_DISCONNECTED', device_name, broadcast=True)
                    print('DISC')

            except queue.Empty:
                pass
            finally:
                socketio.sleep(.1)

    return device_monitor_task


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

    parser.add_argument('--device',
                        required=False,
                        type=str,
                        #default='Telescope Simulator',
                        default='LX200 OnStep',
                        help='Telescope to sync, defaults to "Telescope Simulator"')

    parser.add_argument('--debug',
                        required=False,
                        action='store_true',
                        help='Shows debug messages')

    args = parser.parse_args()

    # Not pretty but works
    SITE_LONGITUDE = args.site_longitude

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig()

    indiclient = SyncClient(device_name=args.device)
    indiclient.setServer(args.host, args.port)

    coords_task = socketio.start_background_task(target=coords_send_task)
    indi_task = socketio.start_background_task(target=indi_monitor_task)
    device_task = socketio.start_background_task(target=build_device_monitor_task(indiclient))
    socketio.start_background_task(indiclient.start)

    @socketio.on('GET_INDI_STATUS')
    def __get_indi_status(data=None):
        socketio.emit('INDI_STATUS', INDI_STATUS)

    @socketio.on('GET_EQUATORIAL_EOD_COORD')
    @socketio.on('GET_SCOPE_STATUS')
    def __get_coords(data=None):
        print (SCOPE_STATUS)
        socketio.emit('EQUATORIAL_EOD_COORD', SCOPE_STATUS)
        socketio.emit('SCOPE_STATUS', SCOPE_STATUS, broadcast=True)

    @socketio.on('START_TRACKING')
    def __start_tracking(data=None):
        indiclient.set_tracking(True)

    @socketio.on('STOP_TRACKING')
    def __stop_tracking(data=None):
        indiclient.set_tracking(False)

    @socketio.on('SYNC_CENTER')
    def __stop_tracking(data=None):
        ra = calc_ra(0)
        indiclient.sync(ra=ra, dec=SCOPE_STATUS['DEC'])

    @socketio.on('MOVE_RELATIVE')
    def __move_relative(offset_ra=0.0, offset_dec=0.0):
        ra = SCOPE_STATUS['RA'] + offset_ra / 15.0
        dec = SCOPE_STATUS['DEC'] + offset_dec
        indiclient.go_to(ra=ra, dec=dec)

    socketio.run(app, host='0.0.0.0')
