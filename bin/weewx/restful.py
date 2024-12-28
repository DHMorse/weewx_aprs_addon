import serial
import time
import weewx
import weeutil.weeutil
from typing import Optional, Dict, Any, Union
from dataclasses import dataclass
from enum import Enum

class APRSError(Exception):
    """Custom exception for APRS-related errors."""
    pass

class SerialConfig:
    """Handles serial port configuration validation."""
    VALID_PARITIES = {'N', 'E', 'O', 'M', 'S'}
    
    @staticmethod
    def validate_parity(parity: str) -> str:
        parity = parity.upper()
        if parity not in SerialConfig.VALID_PARITIES:
            raise ValueError(f"Invalid parity value. Must be one of {SerialConfig.VALID_PARITIES}")
        return parity

class APRSStatus(Enum):
    """Enum for APRS status codes."""
    DISABLED = "Disabled"
    STALE = "Stale"
    INTERVAL_WAIT = "Interval Wait"
    NON_LATEST = "Non Latest Record"
    INVALID_UNITS = "Invalid Units"
    SUCCESS = "Success"

@dataclass
class APRSConfig:
    """Configuration data class for APRS settings."""
    station: str
    latitude: float
    longitude: float
    hardware: str
    port: str
    baudrate: int
    databits: int
    parity: str
    stopbits: int
    unproto: str
    status_message: str
    enabled: bool
    interval: int = 0
    stale: int = 1800
    max_tries: int = 3

    def __post_init__(self):
        """Validate configuration after initialization."""
        self.station = self.station.upper()
        self.parity = SerialConfig.validate_parity(self.parity)
        
        if not -90 <= self.latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90 degrees")
        if not -180 <= self.longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180 degrees")
        if self.interval < 0:
            raise ValueError("Interval cannot be negative")
        if self.stale < 0:
            raise ValueError("Stale threshold cannot be negative")
        if self.max_tries < 1:
            raise ValueError("Max tries must be at least 1")

class APRS:
    """Upload weather data using the APRS protocol."""

    def __init__(self, site: str, **kwargs):
        """Initialize APRS uploader with configuration."""
        self.site = site
        self.config = APRSConfig(
            station=kwargs['station'],
            latitude=float(kwargs['latitude']),
            longitude=float(kwargs['longitude']),
            hardware=kwargs['hardware'],
            port=kwargs['port'],
            baudrate=int(kwargs['baudrate']),
            databits=int(kwargs['databits']),
            parity=kwargs['parity'],
            stopbits=int(kwargs['stopbits']),
            unproto=kwargs['unproto'],
            status_message=kwargs['status_message'],
            enabled=bool(int(kwargs['enabled'])),
            interval=int(kwargs.get('interval', 0)),
            stale=int(kwargs.get('stale', 1800)),
            max_tries=int(kwargs.get('max_tries', 3))
        )
        self._lastpost: Optional[int] = None
        
    def _check_post_conditions(self, archive: Any, time_ts: int) -> APRSStatus:
        """Check if conditions are met for posting data."""
        if not self.config.enabled:
            return APRSStatus.DISABLED

        last_ts = archive.lastGoodStamp()
        if time_ts != last_ts:
            return APRSStatus.NON_LATEST

        how_old = time.time() - time_ts
        if how_old > self.config.stale:
            return APRSStatus.STALE

        if self._lastpost and time_ts - self._lastpost < self.config.interval:
            return APRSStatus.INTERVAL_WAIT

        return APRSStatus.SUCCESS

    def _send_tnc_commands(self, ser: serial.Serial, packet: str) -> None:
        """Send commands to TNC with proper timing and error handling."""
        commands = [
            ("\x03", 1),  # ctrl-C
            (f"mycall {self.config.station}\r", 1),
            (f"unproto {self.config.unproto}\r", 1),
            ("conv\r", 1),
            (f"{packet}\r", 1),
            (f">{self.config.status_message}\r", 1),
            ("\x03", 0)
        ]

        try:
            for cmd, delay in commands:
                ser.write(cmd.encode())
                if delay:
                    time.sleep(delay)
        except serial.SerialException as e:
            raise APRSError(f"Failed to send TNC commands: {str(e)}")

    def format_weather_data(self, record: Dict[str, Any]) -> str:
        """Format weather data according to APRS protocol specifications."""
        time_tt = time.gmtime(record['dateTime'])
        time_str = time.strftime("@%d%H%Mz", time_tt)

        # Position formatting
        lat_str = weeutil.weeutil.latlon_string(self.config.latitude, ('N', 'S'), 'lat')
        lon_str = weeutil.weeutil.latlon_string(self.config.longitude, ('E', 'W'), 'lon')
        latlon_str = f"{lat_str}{lon_str}"

        # Weather metrics formatting
        wind_temp = self._format_wind_temp(record)
        rain = self._format_rain(record)
        baro = self._format_barometer(record)
        humidity = self._format_humidity(record)
        radiation = self._format_radiation(record)
        
        # Hardware identifier
        hardware_str = ".DsVP" if self.config.hardware == "VantagePro" else ".Unkn"

        return f"{time_str}{latlon_str}{wind_temp}{rain}{baro}{humidity}{radiation}{hardware_str}"

    def _format_wind_temp(self, record: Dict[str, Any]) -> str:
        """Format wind and temperature data."""
        values = []
        for field in ('windDir', 'windSpeed', 'windGust', 'outTemp'):
            val = record.get(field)
            values.append(f"{int(val):03d}" if val is not None else "...")
        return f"_{values[0]}/{values[1]}g{values[2]}t{values[3]}"

    def _format_rain(self, record: Dict[str, Any]) -> str:
        """Format rain data."""
        values = []
        for field in ('rain', 'rain24', 'dailyrain'):
            val = record.get(field)
            values.append(f"{int(val * 100):03d}" if val is not None else "...")
        return f"r{values[0]}p{values[1]}P{values[2]}"

    def _format_barometer(self, record: Dict[str, Any]) -> str:
        """Format barometer data."""
        baro = record.get('barometer')
        if baro is None:
            return "b....."
        
        unit_type = weewx.units.getStandardUnitType(record['usUnits'], 'barometer')
        baro_mbar = weewx.units.convert((baro, unit_type[0], unit_type[1]), 'mbar')
        return f"b{int(baro_mbar[0] * 10):05d}"

    def _format_humidity(self, record: Dict[str, Any]) -> str:
        """Format humidity data."""
        humidity = record.get('outHumidity')
        if humidity is None:
            return "h.."
        return f"h{int(humidity):02d}" if humidity < 100.0 else "h00"

    def _format_radiation(self, record: Dict[str, Any]) -> str:
        """Format radiation data."""
        radiation = record.get('radiation')
        if radiation is None:
            return ""
        if radiation < 1000.0:
            return f"L{int(radiation):03d}"
        if radiation < 2000.0:
            return f"l{int(radiation - 1000):03d}"
        return ""

    def postData(self, archive: Any, time_ts: int) -> None:
        """Post weather data to APRS network."""
        status = self._check_post_conditions(archive, time_ts)
        
        if status != APRSStatus.SUCCESS:
            raise "APRS: {status.value}"

        record = self.extractRecordFrom(archive, time_ts)
        
        if record['usUnits'] != weewx.US:
            raise "APRS: Units must be US Customary."

        packet = self.format_weather_data(record)

        try:
            with serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                bytesize=self.config.databits,
                parity=self.config.parity,
                stopbits=self.config.stopbits
            ) as ser:
                ser.flushOutput()
                ser.flushInput()
                self._send_tnc_commands(ser, packet)
                
        except serial.SerialException as e:
            raise APRSError(f"Serial communication error: {str(e)}")
        except Exception as e:
            raise APRSError(f"Unexpected error during APRS upload: {str(e)}")

        self._lastpost = time_ts