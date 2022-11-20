#!/usr/bin/env python3

# import normal packages
import platform
import logging
import sys
import os
import time
import requests # for http GET
from configparser import ConfigParser # for config/ini file
from pathlib import Path
from gi.repository import GLib as gobject

script_path = Path(__file__).absolute()

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

class DbusShellyBaseService:
  def __init__(self, servicename, paths, config, productname='Shelly 1PM', connection='Shelly 1PM HTTP JSON service'):
    self._config = config

    deviceinstance = config.getint('DEFAULT', 'Deviceinstance')
    customname = config.get('DEFAULT', 'CustomName')

    self._base_url = self._getShellyBaseUrl()

    self._dbusservice = VeDbusService(f"{servicename}.http_{deviceinstance:02d}")
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d", servicename, deviceinstance)

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', str(script_path))
    self._dbusservice.add_path('/Mgmt/ProcessVersion', f'Unkown version, and running on Python {platform.python_version()}')
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    #self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
    self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', customname)
    self._dbusservice.add_path('/Connected', 1)

    self._dbusservice.add_path('/Latency', None)
    self._dbusservice.add_path('/FirmwareVersion', 0.1)
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Position', 0) # normaly only needed for pvinverter
    self._dbusservice.add_path('/Serial', self._getShellySerial())
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/StatusCode', 0)  # Dummy path so VRM detects us as a PV-inverter.

    # add path values to dbus
    for path, settings in paths.items():
      self._dbusservice.add_path(path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(250, self._update) # pause 250ms before the next request

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(config.getint('DEFAULT', 'SignOfLifeLog') * 60 * 1000, self._signOfLife)

  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %d", self._lastUpdate)
    logging.info("Last '/Ac/Power': %s", self._dbusservice['/Ac/Power'])
    logging.info("--- End: sign of life ---")
    return True

  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s", path, value)
    return True # accept the change

  def _getShellyBaseUrl(self):
    config = self._config

    # TODO: don't know what that means, but leave the code for now
    accessType = config['DEFAULT']['AccessType']
    if accessType != 'OnPremise':
      raise ValueError(f"AccessType {accessType} is not supported")

    username = config.get("ONPREMISE", "Username")
    password = config.get("ONPREMISE", "Password")
    host = config.get("ONPREMISE", "Host")

    if username or password:
      return f"http://{username}:{password}@{host}/"
    else:
      return f"http://{host}/"

  def _getShellyJson(self, url):
    full_url = self._base_url + url
    request = requests.get(full_url)

    # check for response
    if not request:
        raise ConnectionError(f"No response from Shelly 1PM - [{full_url}]")

    data = request.json()

    if not data:
        raise ValueError("Converting response to JSON failed")

    return data

  def _getShellySerial(self):
    raise NotImplementedError

  def _getShellyData(self):
    raise NotImplementedError

  def _update(self):
    try:
      total_power, total_energy = self._getShellyData()

      #logging
      logging.debug("Power: %d, Energy: %d", total_power, total_energy)

      # increment UpdateIndex - to show that new data is available (overflow from 255 to 0)
      self._dbusservice['/UpdateIndex'] = (self._dbusservice['/UpdateIndex'] + 1) % 256

      #update lastupdate vars
      self._lastUpdate = time.time()
    except Exception as e:
      logging.critical('Error at %s', '_update', exc_info=e)

    # return true, otherwise add_timeout will be removed from GObject
    return True


class DbusShelly1PMService(DbusShellyBaseService):
  "Shelly 1 PM"

  def __init__(self, servicename, paths, config):
    super().__init__(servicename, paths, config, productname='Shelly 1PM', connection='Shelly 1PM HTTP JSON service')

  def _getShellySerial(self):
    return self._getShellyJson("status")["mac"]

  def _getShellyData(self):
    meter_input = self._config.getint("ONPREMISE", "Input", fallback=0)
    pvinverter_phase = self._config.get('DEFAULT', 'Phase')
    meter_data = self._getShellyJson("status")

    total_power = 0
    total_energy = 0
    for phase in ('L1', 'L2', 'L3'):
      if phase == pvinverter_phase:
        power = meter_data['meters'][meter_input]['power']
        energy = meter_data['meters'][meter_input]['total'] # energy counter in Watt-minute
        voltage = 230
        current = power / voltage

      else:
        power = 0
        energy = 0
        voltage = 0
        current = 0

      total_power += power
      total_energy += energy

      # send data to DBus
      self._dbusservice[f'/Ac/{phase}/Voltage'] = voltage
      self._dbusservice[f'/Ac/{phase}/Current'] = current
      self._dbusservice[f'/Ac/{phase}/Power'] = power
      self._dbusservice[f'/Ac/{phase}/Energy/Forward'] = energy / 1000 / 60

    self._dbusservice['/Ac/Power'] = total_power
    self._dbusservice['/Ac/Energy/Forward'] = total_energy / 1000 / 60

    return total_power, total_energy


class DbusShelly1PlusPMService(DbusShellyBaseService):
  "Shelly Plus 1 PM"

  def __init__(self, servicename, paths, config):
    super().__init__(servicename, paths, config, productname='Shelly Plus 1PM', connection='Shelly Plus 1PM HTTP JSON service')

  def _getShellySerial(self):
    return self._getShellyJson("rpc/Sys.GetStatus")["mac"]

  def _getShellyData(self):
    meter_input = self._config.getint("ONPREMISE", "Input", fallback=0)
    pvinverter_phase = self._config.get('DEFAULT', 'Phase')
    meter_data = self._getShellyJson(f"rpc/Switch.GetStatus?id={meter_input}")

    total_power = 0
    total_energy = 0
    for phase in ('L1', 'L2', 'L3'):
      active_phase = phase == pvinverter_phase

      if active_phase:
        power = meter_data['apower']
        energy = meter_data['aenergy']["total"] # energy counter in Watt-hour
        voltage = meter_data['voltage']
        current = meter_data['current']

      else:
        power = 0
        energy = 0
        voltage = 0
        current = 0

      total_power += power
      total_energy += energy

      # send data to DBus
      self._dbusservice[f'/Ac/{phase}/Voltage'] = voltage
      self._dbusservice[f'/Ac/{phase}/Current'] = current
      self._dbusservice[f'/Ac/{phase}/Power'] = power
      self._dbusservice[f'/Ac/{phase}/Energy/Forward'] = energy / 1000

    self._dbusservice['/Ac/Power'] = total_power
    self._dbusservice['/Ac/Energy/Forward'] = total_energy / 1000

    return total_power, total_energy

def main():
  config = ConfigParser()
  config.read(str(script_path.parent / "config.ini"))

  logging_handlers = [logging.StreamHandler()]
  logging_file = config.get("DEFAULT", "LogFile")
  if logging_file:
    logging_handlers.append(logging.FileHandler(str(script_path.parent / logging_file)))

  #configure logging
  logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                      datefmt='%Y-%m-%d %H:%M:%S',
                      level=logging.INFO,
                      handlers=logging_handlers)

  try:
      logging.info("Start")

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)

      #formatting
      _kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')

      shelly_plus = config.getboolean("DEFAULT", "ShellyPlus")
      service_class = DbusShelly1PlusPMService if shelly_plus else DbusShelly1PMService

      #start our main-service
      pvac_output = service_class(
        servicename='com.victronenergy.pvinverter',
        paths={
          '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Current': {'initial': 0, 'textformat': _a},
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L2/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L3/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L2/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L3/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L2/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L3/Energy/Forward': {'initial': None, 'textformat': _kwh},
        },
        config=config)

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)

if __name__ == "__main__":
  main()
