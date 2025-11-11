import enum
import logging
from collections import namedtuple
from typing import TYPE_CHECKING, Any, Literal, Optional

from octoprint.events import Events, eventManager
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.storage import StorageCapabilities
from octoprint.printer import JobProgress, PrinterFile, PrinterFilesMixin
from octoprint.printer.connection import (
    ConnectedPrinter,
    ConnectedPrinterListenerMixin,
    ConnectedPrinterState,
)
from octoprint.printer.job import PrintJob
from octoprint.schema import BaseModel

from .vendor import bpm

GCODE_STATE_LOOKUP = {
    "FAILED": ConnectedPrinterState.ERROR,
    "FINISH": ConnectedPrinterState.OPERATIONAL,
    "IDLE": ConnectedPrinterState.OPERATIONAL,
    "INIT": ConnectedPrinterState.CONNECTING,
    "OFFLINE": ConnectedPrinterState.CLOSED,
    "PAUSE": ConnectedPrinterState.PAUSED,
    "PREPARE": ConnectedPrinterState.TRANSFERRING_FILE,
    "RUNNING": ConnectedPrinterState.PRINTING,
    "UNKNOWN": ConnectedPrinterState.CLOSED,
}

RELEVANT_EXTENSIONS = (".gcode", ".gco", ".gcode.3mf")
IGNORED_FOLDERS = ("/logger", "/recorder", "/timelapse", "/image", "/ipcam")
MODELS_SDCARD_MOUNT = ()


class PrintStatsSupplemental(BaseModel):
    total_layer: Optional[int] = None
    current_layer: Optional[int] = None


class PrintStats(BaseModel):
    filename: Optional[str] = None

    total_duration: Optional[float] = None
    """Elapsed time since start"""

    print_duration: Optional[float] = None
    """Total duration minus time until first extrusion and pauses, see https://github.com/Klipper3d/klipper/blob/9346ad1914dc50d12f1e5efe630448bf763d1469/klippy/extras/print_stats.py#L112"""

    filament_used: Optional[float] = None

    state: Optional[
        Literal["standby", "printing", "paused", "complete", "error", "cancelled"]
    ] = None

    message: Optional[str] = None

    info: Optional[PrintStatsSupplemental] = None


class SDCardStats(BaseModel):
    file_path: Optional[str] = (
        None  # unset if no file is loaded, path is the path on the file system
    )
    progress: Optional[float] = None  # 0.0 to 1.0
    is_active: Optional[bool] = None  # True if a print is ongoing
    file_position: Optional[int] = None
    file_size: Optional[int] = None


class IdleTimeout(BaseModel):
    state: Optional[Literal["Printing", "Ready", "Idle"]] = (
        None  # "Printing" means some commands are being executed!
    )
    printing_time: Optional[float] = (
        None  # Duration of "Printing" state, resets on state change to "Ready"
    )


Coordinate = namedtuple("Coordinate", "x, y, z, e")


class PositionData(BaseModel):
    speed_factor: Optional[float] = None
    speed: Optional[float] = None
    extruder_factor: Optional[float] = None
    absolute_coordinates: Optional[bool] = None
    absolute_extrude: Optional[bool] = None
    homing_origins: Optional[Coordinate] = None  # offsets
    position: Optional[Coordinate] = None  # current w/ offsets
    gcode_position: Optional[Coordinate] = None  # current w/o offsets


class TemperatureDataPoint:
    actual: float = 0.0
    target: float = 0.0

    def __init__(self, actual: float = 0.0, target: float = 0.0):
        self.actual = actual
        self.target = target

    def __str__(self):
        return f"{self.actual} / {self.target}"

    def __repr__(self):
        return f"TemperatureDataPoint({self.actual}, {self.target})"


class BambuState(enum.Enum):
    READY = "ready"
    ERROR = "error"
    SHUTDOWN = "shutdown"
    STARTUP = "startup"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"

    @classmethod
    def for_value(cls, value: str) -> "BambuState":
        for state in cls:
            if state.value == value:
                return state
        return BambuState.UNKNOWN


class PrinterState(enum.Enum):
    STANDBY = "standby"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    RUNNING = "running"
    OPERATIONAL = "FINISH"

    @classmethod
    def for_value(cls, value: str) -> "PrinterState":
        for state in cls:
            if state.value == value:
                return state
        return cls.UNKNOWN


class IdleState(enum.Enum):
    PRINTING = "Printing"
    READY = "Ready"
    IDLE = "Idle"
    UNKNOWN = "unknown"

    @classmethod
    def for_value(cls, value: str) -> "IdleState":
        for state in cls:
            if state.value == value:
                return state
        return cls.UNKNOWN


if TYPE_CHECKING:
    from octoprint.events import EventManager
    from octoprint.filemanager import FileManager
    from octoprint.plugin import PluginManager, PluginSettings


class ConnectedBambuPrinter(
    ConnectedPrinter, PrinterFilesMixin, ConnectedPrinterListenerMixin
):
    connector = "bambu"
    name = "Bambu (local)"

    storage_capabilities = StorageCapabilities(
        write_file=True,
        read_file=True,
        remove_file=True,
        copy_file=False,
        move_file=False,
        add_folder=False,
        remove_folder=False,
        copy_folder=False,
        move_folder=False,
    )

    can_set_job_on_hold = False

    @classmethod
    def connection_options(cls) -> dict:
        return {}

    TEMPERATURE_LOOKUP = {
        "extruder": "tool0",
        "heater_bed": "bed",
        "chamber": "chamber",
    }

    # injected by our plugin
    _event_bus: "EventManager" = None
    _file_manager: "FileManager" = None
    _plugin_manager: "PluginManager" = None
    _plugin_settings: "PluginSettings" = None
    # /injected

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logger = logging.getLogger(__name__)

        self._host = kwargs.get("host")
        self._serial = kwargs.get("serial")
        self._access_code = kwargs.get("access_code")

        self._client = None

        self._state = ConnectedPrinterState.CLOSED
        self._error = None

        self._progress: JobProgress = None
        self._job_cache: str = None

        self._files: list[PrinterFile] = []

        self._printer_state: PrinterState = PrinterState.UNKNOWN
        self._idle_state: IdleState = IdleState.UNKNOWN
        self._position: Coordinate = None

    @property
    def connection_parameters(self):
        parameters = super().connection_parameters
        parameters.update(
            {
                "host": self._host,
                "serial": self._serial,
                "access_code": self._access_code,
            }
        )
        return parameters

    @classmethod
    def connection_preconditions_met(cls, params):
        from octoprint.util.net import resolve_host

        host = params.get("host")
        serial = params.get("serial")
        access_code = params.get("access_code")

        return host and resolve_host(host) and serial and access_code

    def set_state(self, state: ConnectedPrinterState, error: str = None):
        if state == self.state:
            return

        old_state = self.state

        if (
            old_state == ConnectedPrinterState.CONNECTING
            and state == ConnectedPrinterState.OPERATIONAL
        ):
            self._listener.on_printer_files_available(True)
            self._listener.on_printer_files_refreshed(
                self.get_printer_files(refresh=True)
            )
            self._logger.info(f"Files: {self._files}")
            pass

        super().set_state(state, error=error)

        message = f"State changed from {old_state.name} to {self.state.name}"
        self._logger.info(message)
        self._listener.on_printer_logs(message)

    @property
    def job_progress(self) -> JobProgress:
        return self._progress

    def connect(self, *args, **kwargs):
        from . import BambuRolloverLogHandler

        if (
            self._client is not None
            or self._host == ""
            or self._serial == ""
            or self._access_code == ""
        ):
            return

        BambuRolloverLogHandler.arm_rollover()

        eventManager().fire(Events.CONNECTING)
        self.set_state(ConnectedPrinterState.CONNECTING)

        try:
            self._logger.info("Connecting to Bambu")

            config = bpm.bambuconfig.BambuConfig(
                hostname=self._host,
                access_code=self._access_code,
                serial_number=self._serial,
            )
            printer = bpm.bambuprinter.BambuPrinter(config=config)

            printer.on_update = self._on_bpm_update

            printer.start_session()
        except Exception as e:
            self._logger.exception(e)
            self.set_state(ConnectedPrinterState.CLOSED_WITH_ERROR, f"{e}")
            return False

        self._client = printer
        return True

    def disconnect(self, *args, **kwargs):
        if self._client is None:
            return
        eventManager().fire(Events.DISCONNECTING)
        self._client.quit()
        self.set_state(ConnectedPrinterState.CLOSED)

    def emergency_stop(self, *args, **kwargs):
        self.commands("M112", tags=kwargs.get("tags", set()))

    def get_error(self, *args, **kwargs):
        return self._error

    def jog(self, axes, relative=True, speed=None, *args, **kwargs):
        command = "G0 {}".format(
            " ".join([f"{axis.upper()}{amt}" for axis, amt in axes.items()])
        )

        if speed is None:
            speed = min(self._profile["axes"][axis]["speed"] for axis in axes)

        if speed and not isinstance(speed, bool):
            command += f" F{speed}"

        if relative:
            commands = ["G91", command, "G90"]
        else:
            commands = ["G90", command]

        self.commands(
            *commands, tags=kwargs.get("tags", set()) | {"trigger:connector.jog"}
        )

    def home(self, axes, *args, **kwargs):
        self.commands(
            "G91",
            "G28 {}".format(" ".join(f"{x.upper()}0" for x in axes)),
            "G90",
            tags=kwargs.get("tags", set) | {"trigger:connector.home"},
        )

    def extrude(self, amount, speed=None, *args, **kwargs):
        # Use specified speed (if any)
        max_e_speed = self._profile["axes"]["e"]["speed"]

        if speed is None:
            # No speed was specified so default to value configured in printer profile
            extrusion_speed = max_e_speed
        else:
            # Make sure that specified value is not greater than maximum as defined in printer profile
            extrusion_speed = min([speed, max_e_speed])

        self.commands(
            "G91",
            "M83",
            f"G1 E{amount} F{extrusion_speed}",
            "M82",
            "G90",
            tags=kwargs.get("tags", set()) | {"trigger:connector.extrude"},
        )

    def change_tool(self, tool, *args, **kwargs):
        tool = int(tool[len("tool") :])
        self.commands(
            f"T{tool}",
            tags=kwargs.get("tags", set()) | {"trigger:connector.change_tool"},
        )

    def set_temperature(self, heater, value, tags=None, *args, **kwargs):
        if not tags:
            tags = set()
        tags |= {"trigger:connector.set_temperature"}

        if heater == "tool":
            # set current tool, whatever that might be
            self.commands(f"M104 S{value}", tags=tags)

        elif heater.startswith("tool"):
            # set specific tool
            extruder_count = self._profile["extruder"]["count"]
            shared_nozzle = self._profile["extruder"]["sharedNozzle"]
            if extruder_count > 1 and not shared_nozzle:
                toolNum = int(heater[len("tool") :])
                self.commands(f"M104 T{toolNum} S{value}", tags=tags)
            else:
                self.commands(f"M104 S{value}", tags=tags)

        elif heater == "bed":
            self.commands(f"M140 S{value}", tags=tags)

        elif heater == "chamber":
            self.commands(f"M141 S{value}", tags=tags)

    def commands(self, *commands, tags=None, force=False, **kwargs):
        if self._client is None:
            return

        self._client.send_gcode("\n".join(commands))

    def is_ready(self, *args, **kwargs):
        if not self._client:
            return False

        return (
            super().is_ready(*args, **kwargs)
            and self.state == ConnectedPrinterState.OPERATIONAL
        )

    # ~~ Job handling

    def supports_job(self, job: PrintJob) -> bool:
        return job.storage == FileDestinations.PRINTER

    def start_print(self, pos=None, user=None, tags=None, *args, **kwargs):
        raise NotImplementedError()

    def pause_print(self, tags=None, *args, **kwargs):
        raise NotImplementedError()

    def resume_print(self, tags=None, *args, **kwargs):
        raise NotImplementedError()

    def cancel_print(self, tags=None, *args, **kwargs):
        raise NotImplementedError()

    # ~~ PrinterFilesMixin

    @property
    def printer_files_mounted(self) -> bool:
        return self._client is not None

    def refresh_printer_files(
        self, blocking=False, timeout=10, *args, **kwargs
    ) -> None:
        if not self._client or not self._client.connected:
            return

        raise NotImplementedError()

    def get_printer_files(self, refresh=False, recursive=False, *args, **kwargs):
        if not self.printer_files_mounted:
            return []

        if not self._files or refresh:
            files = self._client.get_sdcard_contents()
            self._files = self._to_printer_files(files.get("children", []))

        return self._files

    def create_printer_folder(self, target: str, *args, **kwargs) -> None:
        raise NotImplementedError()

    def delete_printer_folder(
        self, target: str, recursive: bool = False, *args, **kwargs
    ):
        raise NotImplementedError()

    def copy_printer_folder(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def move_printer_folder(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def upload_printer_file(
        self, path_or_file, path, upload_callback, *args, **kwargs
    ) -> str:
        raise NotImplementedError()

    def download_printer_file(self, path, *args, **kwargs):
        raise NotImplementedError()

    def delete_printer_file(self, path, *args, **kwargs):
        raise NotImplementedError()

    def copy_printer_file(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def move_printer_file(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    # ~~ BPM callback

    def _on_bpm_update(self, printer: bpm.bambuprinter.BambuPrinter):
        if printer != self._client:
            return

        if self.state == ConnectedPrinterState.CONNECTING:
            self.state = ConnectedPrinterState.OPERATIONAL
            eventManager().fire(
                Events.CONNECTED,
                {
                    "connector": self.name,
                    "host": self._host,
                    "serial": self._serial,
                    "access_code": self._access_code is not None,
                },
            )

        # self._evaluate_state(printer)

        self._listener.on_printer_temperature_update(
            {
                "tool0": (printer.tool_temp, printer.tool_temp_target),
                "bed": (printer.bed_temp, printer.bed_temp_target),
                "chamber": (printer.chamber_temp, printer.chamber_temp_target),
            }
        )

    ##~~ helpers

    def _evaluate_actual_status(self):
        if self.state in (
            ConnectedPrinterState.STARTING,
            ConnectedPrinterState.RESUMING,
        ):
            if self._printer_state != PrinterState.PRINTING:
                # not yet printing
                return

            if self.state == ConnectedPrinterState.STARTING:
                self._listener.on_printer_job_started()
            else:
                self._listener.on_printer_job_resumed()
            self.state = ConnectedPrinterState.PRINTING

        elif self.state in (
            ConnectedPrinterState.FINISHING,
            ConnectedPrinterState.CANCELLING,
            ConnectedPrinterState.PAUSING,
        ):
            if self._idle_state == IdleState.PRINTING:
                # still printing
                return

            if (
                self.state == ConnectedPrinterState.FINISHING
                and self._printer_state
                in (
                    PrinterState.COMPLETE,
                    PrinterState.STANDBY,
                )
            ):
                # print done
                self._progress.progress = 1.0
                self._listener.on_printer_job_done()
                self.state = ConnectedPrinterState.OPERATIONAL
            elif (
                self.state == ConnectedPrinterState.CANCELLING
                and self._printer_state
                in (PrinterState.CANCELLED, PrinterState.ERROR, PrinterState.STANDBY)
            ):
                # print failed
                self._listener.on_printer_job_cancelled()
                self.state = ConnectedPrinterState.OPERATIONAL
            elif (
                self.state == ConnectedPrinterState.PAUSING
                and self._printer_state == PrinterState.PAUSED
            ):
                # print paused
                self._listener.on_printer_job_paused()
                self.state = ConnectedPrinterState.PAUSED

    def _to_printer_files(self, nodes: list[dict[str, Any]]) -> list[PrinterFile]:
        result = []
        for node in nodes:
            if "children" in node:
                result += self._to_printer_files(node["children"])
            else:
                result.append(
                    PrinterFile(
                        path=node["id"][1:],  # strip leading /
                        display=node["name"],
                        size=node.get("size", 0),
                        date=int(node.get("timestamp", 0)),
                    )
                )
        return result
