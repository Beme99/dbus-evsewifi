#!/usr/bin/env python

# import normal packages
import platform
import logging
import os
import sys

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests  # for http GET
import configparser  # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusEvseWifiService:
    def __init__(self, servicename, paths, productname='EVSE-WiFi', connection='EVSE-WiFi JSON API'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        paths_wo_unit = [
            '/Status',
            '/Mode'
        ]

        # get data from go-eCharger
        data = self._getEvseWifiData()

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion',
                                   'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)  #
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', productname)
        self._dbusservice.add_path('/HardwareVersion', 2)
        self._dbusservice.add_path('/FirmwareVersion', 'Unknown')
        self._dbusservice.add_path('/Serial', 1)
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', int(config['DEFAULT']['ACPosition'])) # 0: AC-Output / 1: AC-Input

        # add paths without units
        for path in paths_wo_unit:
            self._dbusservice.add_path(path, None)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=True,
                onchangecallback=self._handlechangedvalue)

        # last update
        self._lastUpdate = 0

        # charging time in float
        self._chargingTime = 0.0

        # add _update function 'timer'
        gobject.timeout_add(10000, self._update)  # pause 2sec before the next request

        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT']['SignOfLifeLogInterval']

        if not value:
            value = 0

        return int(value)

    def _getEvseWifiStatusUrl(self):
        config = self._getConfig()
        URL = "http://%s/getParameters" % (config['DEFAULT']['Host'])
        return URL

    def _getEvseWifiMqttPayloadUrl(self, parameter, value):
        config = self._getConfig()
        URL = "http://%s/setCurrent?%s=%s" % (config['DEFAULT']['Host'], parameter, value)
        return URL

    def _setEvseWifiValue(self, parameter, value):
        URL = self._getEvseWifiMqttPayloadUrl(parameter, str(value))
        request_data = requests.get(url=URL)

        # check for response
        if not request_data:
            raise ConnectionError("No response from Evse-Charger - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        if json_data[parameter] == str(value):
            return True
        else:
            logging.warning("Evse-Charger parameter %s not set to %s" % (parameter, str(value)))
            return False

    def _getEvseWifiData(self):
        URL = self._getEvseWifiStatusUrl()
        request_data = requests.get(url=URL)

        # check for response
        if not request_data:
            raise ConnectionError("No response from EVSE-WiFi - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            config = self._getConfig()
            
            # get data from go-eCharger
            datacomplete = self._getEvseWifiData()
            data=datacomplete["list"][0]
            
            # send data to DBus        
            self._dbusservice['/Ac/L1/Power'] = float((data['actualPower']*1000) / 3.0)
            self._dbusservice['/Ac/L2/Power'] = float((data['actualPower']*1000) / 3.0)
            self._dbusservice['/Ac/L3/Power'] = float((data['actualPower']*1000) / 3.0)
            self._dbusservice['/Ac/Power'] = int(data['actualPower']*1000)
            self._dbusservice['/Ac/Voltage'] = 230
            self._dbusservice['/Current'] = int(data['actualCurrent'])
            self._dbusservice['/Ac/Energy/Forward'] = float(data['energy'])
            if int(data['actualCurrent']) == 0:
                self._dbusservice['/StartStop'] = 0
            else:
                self._dbusservice['/StartStop'] = 1

            self._dbusservice['/SetCurrent'] = int(data['actualCurrent'])
            self._dbusservice['/MaxCurrent'] = int(data['maxCurrent'])

            self._dbusservice['/ChargingTime'] = int(data['duration'] / 1000)

            self._dbusservice['/Mode'] = int(config['DEFAULT']['automaticMode'])
            
	# 'vehicleState' EVSE-WiFi states: Fahrzeugstatus (1: bereit | 2: Fahrzeug angeschlossen | 3: Fahrzeug l??dt | 5: Fehler)
	# Victron states: 0:EVdisconnected; 1:Connected; 2:Charging; 3:Charged; 4:Wait sun; 5:Wait RFID; 6:Wait enable; 7:Low SOC; 8:Ground error; 9:Welded contacts error; defaut:Unknown;
            status = 0
            if int(data['vehicleState']) == 1 :
                status = 0
            elif int(data['vehicleState']) == 2 and int(data['actualCurrent']) == 0:
                status = 1
            elif int(data['vehicleState']) == 2 and int(data['actualCurrent']) > 0:
                status = 3
            elif int(data['vehicleState']) == 3:
                status = 2
            elif int(data['vehicleState']) == 5:
                status = 8
            self._dbusservice['/Status'] = status

            # logging
            logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
            logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
            logging.debug("---")

            # increment UpdateIndex - to show that new data is available
            index = self._dbusservice['/UpdateIndex'] + 1  # increment index
            if index > 255:  # maximum value of the index
                index = 0  # overflow from 255 to 0
            self._dbusservice['/UpdateIndex'] = index

            # update lastupdate vars
            self._lastUpdate = time.time()
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.info("someone else updated %s to %s" % (path, value))

        if path == '/SetCurrent':
            return self._setEvseWifiValue('current', value)
        elif path == '/StartStop':
            if value == 0:
                return self._setEvseWifiValue('current', '0') #Stop
            elif value == 1:
                return self._setEvseWifiValue('current', int(self._dbusservice['/MaxCurrent'])) #Start
        else:
            logging.info("mapping for evcharger path %s does not exist" % (path))
            return False


def main():
    # configure logging
    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.INFO,
                        handlers=[
                            logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                            logging.StreamHandler()
                        ])

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop
        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        _w = lambda p, v: (str(round(v, 1)) + 'W')
        _v = lambda p, v: (str(round(v, 1)) + 'V')
        _degC = lambda p, v: (str(v) + '??C')
        _s = lambda p, v: (str(v) + 's')

        # start our main-service
        pvac_output = DbusEvseWifiService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Ac/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/ChargingTime': {'initial': 0, 'textformat': _s},

                '/Ac/Voltage': {'initial': 0, 'textformat': _v},
                '/Current': {'initial': 0, 'textformat': _a},
                '/SetCurrent': {'initial': 0, 'textformat': _a},
                '/MaxCurrent': {'initial': 0, 'textformat': _a},
                '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))}
            }
        )

        logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()
