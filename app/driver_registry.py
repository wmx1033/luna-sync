"""Registry used by the application to choose a camera protocol adapter."""

from drivers.go_ultra import GoUltraDriver
from drivers.luna_ultra import LunaUltraDriver


_DRIVERS = {
    GoUltraDriver.id: GoUltraDriver,
    LunaUltraDriver.id: LunaUltraDriver,
}


def register_driver(driver_type, factory):
    if not driver_type:
        raise ValueError('driver type is required')
    _DRIVERS[driver_type] = factory


def driver_class(driver_type):
    try:
        return _DRIVERS[driver_type]
    except KeyError as exc:
        raise ValueError('unsupported camera driver: ' + str(driver_type)) from exc


def create_driver(driver_type, host):
    return driver_class(driver_type)(host)


def create_driver_for_device(device):
    return create_driver(device['driver'], device['camera_host'])


def device_endpoint(device):
    """The address the network layer must make reachable, without opening a session."""
    return device['camera_host'], driver_class(device['driver']).probe_port


def available_drivers():
    return tuple(sorted(_DRIVERS))


def driver_catalog():
    return [
        {
            'id': driver.id,
            'display_name': driver.display_name,
            'capabilities': driver.capabilities.as_dict(),
        }
        for _, driver in sorted(_DRIVERS.items())
    ]
