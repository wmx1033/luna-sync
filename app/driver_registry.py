"""Registry used by the application to choose a camera protocol adapter."""

from drivers.luna_ultra import LunaUltraDriver


_DRIVERS = {
    LunaUltraDriver.id: LunaUltraDriver,
}


def register_driver(driver_type, factory):
    if not driver_type:
        raise ValueError('driver type is required')
    _DRIVERS[driver_type] = factory


def create_driver(driver_type, host):
    try:
        factory = _DRIVERS[driver_type]
    except KeyError as exc:
        raise ValueError('unsupported camera driver: ' + str(driver_type)) from exc
    return factory(host)


def create_driver_for_device(device):
    return create_driver(device['driver'], device['camera_host'])


def available_drivers():
    return tuple(sorted(_DRIVERS))
