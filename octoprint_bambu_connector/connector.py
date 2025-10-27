import enum
import ftplib
import logging
import os
import threading
from collections import namedtuple
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import Literal, Optional, cast

from octoprint.events import Events, eventManager
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.storage import StorageCapabilities
from octoprint.printer import JobProgress, PrinterFile, PrinterFilesMixin
from octoprint.printer.connection import (
    ConnectedPrinter,
    ConnectedPrinterListenerMixin,
    ConnectedPrinterState,
    FirmwareInformation,
)
from octoprint.printer.job import PrintJob
from octoprint.schema import BaseModel

from .vendor.pybambu.bambu_client import BambuClient
from .vendor.pybambu.commands import PAUSE, RESUME, STOP
from .vendor.pybambu.const import Printers
from .worker import AsyncTaskWorker

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
MODELS_SDCARD_MOUNT = (Printers.X1, Printers.X1C, Printers.X1E)


class FileInfo(BaseModel):
    path: str
    modified: float
    size: int
    permissions: str


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from octoprint.server import fileManager, pluginManager
        from octoprint.settings import settings

        self._logger = logging.getLogger(__name__)
        self._event_manager = eventManager()
        self._file_manager = fileManager
        self._plugin_manager = pluginManager
        self._settings = settings()

        self._host = kwargs.get("host")
        self._serial = kwargs.get("serial")
        self._access_code = kwargs.get("access_code")

        self._client = None

        self._state = ConnectedPrinterState.CLOSED
        self._error = None

        self._progress: JobProgress = None
        self._job_cache: str = None

        self._files: list[FileInfo] = []

        self._printer_state: PrinterState = PrinterState.UNKNOWN
        self._idle_state: IdleState = IdleState.UNKNOWN
        self._position: Coordinate = None

        # create a thread pool for asyncio tasks
        self.worker = AsyncTaskWorker()

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

        super().set_state(state, error=error)

        message = f"State changed from {old_state.name} to {self.state.name}"
        self._logger.info(message)
        self._listener.on_printer_logs(message)

    @property
    def job_progress(self) -> JobProgress:
        return self._progress

    def on_bambu_client_update(self, event_type):
        if self._client is not None and self._client.connected:
            if event_type == "event_hms_errors":
                self._update_hms_errors()
            elif event_type == "event_printer_data_update":
                self._update_printer_info()

    def _update_hms_errors(self):
        pass

    def _update_printer_info(self):
        device_data = self._client.get_device()
        print_job_state = GCODE_STATE_LOOKUP[device_data.print_job.gcode_state.upper()]
        temperatures = device_data.temperature

        if print_job_state == ConnectedPrinterState.OPERATIONAL:
            self.set_state(ConnectedPrinterState.OPERATIONAL)
        if print_job_state == ConnectedPrinterState.PRINTING:
            self.set_state(ConnectedPrinterState.PRINTING)
            if not self._progress:
                self._progress = JobProgress()
            self._progress.progress = device_data.print_job.print_percentage
            self._listener.on_printer_job_progress()

        temperature_data = {
            "tool0": (
                temperatures.active_nozzle_temperature,
                temperatures.active_nozzle_target_temperature,
            ),
            "bed": (temperatures.bed_temp, temperatures.target_bed_temp),
            "chamber": (temperatures.chamber_temp, 0.0),
        }

        self._listener.on_printer_temperature_update(temperature_data)

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
        self._client = BambuClient(
            {
                "host": self._host,
                "serial": self._serial,
                "access_code": self._access_code,
                "enable_camera": False,
                "local_mqtt": True,
            }
        )

        self._logger.info("Connecting to Bambu")
        try:
            if self.worker.loop.is_running():
                future = self.worker.run_coroutine_threadsafe(
                    self._client.try_connection()
                )
                success = future.result()
                if success:
                    self.worker.run_coroutine_threadsafe(
                        self._client.connect(self.on_bambu_client_update)
                    )
                else:
                    self.set_state(ConnectedPrinterState.CLOSED)
            else:
                self._logger.debug("loop not running")
                return False
        except Exception as e:
            self._logger.exception(e)
            self.set_state(ConnectedPrinterState.CLOSED_WITH_ERROR, f"{e}")
            return False
        return True

    def disconnect(self, *args, **kwargs):
        if self._client is None:
            return
        eventManager().fire(Events.DISCONNECTING)
        self._client.disconnect()
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

        msg = {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": "\n".join(commands),
            }
        }
        self._client.publish(msg)

    def is_ready(self, *args, **kwargs):
        if not self._client:
            return False

        return (
            super().is_ready(*args, **kwargs)
            and self.state == ConnectedPrinterState.OPERATIONAL
        )

    # ~~ Job handling

    def supports_job(self, job: PrintJob) -> bool:
        # if not valid_file_type(job.path, type="machinecode"):
        #    return False
        #
        # if (
        #    job.storage != FileDestinations.PRINTER
        #    and not self._file_manager.capabilities(job.storage).read_file
        # ):
        #    return False
        #
        # return True

        return job.storage == FileDestinations.PRINTER

    def start_print(self, pos=None, user=None, tags=None, *args, **kwargs):
        if pos is None:
            pos = 0

        self.state = ConnectedPrinterState.STARTING
        self._progress = JobProgress(
            job=self.current_job,
            progress=0.0,
            pos=pos,
            elapsed=0.0,
            cleaned_elapsed=0.0,
        )

        try:
            if self.current_job.storage == FileDestinations.PRINTER:
                if not self._start_current_job_on_printer():
                    raise RuntimeError("Print start unsuccessful")

            """
            else:
                # we first need to upload this as a cache file, then start the print on that

                if self._job_cache:
                    # if we still have a job cache file, delete it now
                    for f in self._job_cache:
                        self._client.delete_file(f)
                    self._job_cache = []

                _, filename = self._file_manager.split_path(
                    self.current_job.storage, self.current_job.path
                )
                job_cache = f".octoprint/{filename}"
                print_future = Future()

                def handle_uploaded(future: Future) -> None:
                    try:
                        future.result()

                        self._client.start_print(job_cache).result()

                        print_future.set_result(True)

                    except Exception as exc:
                        print_future.set_exception(exc)

                handle = self._file_manager.read_file(
                    self.current_job.storage, self.current_job.path
                )
                self._client.upload_file(handle, job_cache).add_done_callback(
                    handle_uploaded
                )
                print_future.result()
            """

        except Exception:
            self._logger.exception(
                f"Error while starting print job of {self.current_job.storage}:{self.current_job.path}"
            )
            self._listener.on_printer_job_cancelled()
            self.state = ConnectedPrinterState.OPERATIONAL

    def pause_print(self, tags=None, *args, **kwargs):
        self.state = ConnectedPrinterState.PAUSING
        self._client.publish(PAUSE)

    def resume_print(self, tags=None, *args, **kwargs):
        self.state = ConnectedPrinterState.RESUMING
        self._client.publish(RESUME)

    def cancel_print(self, tags=None, *args, **kwargs):
        self.state = ConnectedPrinterState.CANCELLING
        self._client.publish(STOP)

    def _start_current_job_on_printer(self):
        from urllib.parse import quote_plus

        fs_root = "/"
        if self._client.get_device().info.device_type in MODELS_SDCARD_MOUNT:
            fs_root = "/mnt/sdcard/"

        job = self.current_job
        if job.path.endswith(".gcode"):
            print_command = {
                "print": {
                    "sequence_id": "0",
                    "command": "gcode_file",
                    "param": f"{fs_root}{job.path}",
                }
            }

        else:
            url = f"file://{fs_root}" + "/".join(map(quote_plus, job.path.split("/")))

            print_command = {
                "print": {
                    "sequence_id": "2342",
                    "command": "project_file",
                    "param": "Metadata/plate_X.gcode",
                    "profile_id": "0",  # always 0 for local prints
                    "project_id": "0",  # always 0 for local prints
                    "task_id": "0",  # always 0 for local prints
                    "subtask_id": "0",  # always 0 for local prints
                    "subtask_name": "",
                    "file": "",  # filename to print, not needed when "url" is specified
                    "url": url,  # URL to print, root path,
                    "md5": "",
                    # TODO: needs to be taken from some settings *somewhere*
                    "timelapse": False,
                    "bed_type": "auto",
                    "bed_leveling": True,
                    "flow_cali": True,
                    "vibration_cali": True,
                    "layer_inspect": False,
                    "ams_mapping": "",
                    "use_ams": False,
                }
            }

        return self._client.publish(print_command)

    # ~~ PrinterFilesMixin

    @property
    def printer_files_mounted(self) -> bool:
        return self._client is not None and self._client.ftp_enabled

    def _recursive_ftp_list(self, ftp: ftplib.FTP, path="/") -> list[FileInfo]:
        result = []

        try:
            file_list = ftp.nlst(path)
        except ftplib.error_perm:
            return result
        except Exception as e:
            self._logger.warning(f"Error retrieving file list for {path}: {e}")
            return result

        for file_name in file_list:
            file_path = os.path.join(path, file_name)

            if file_path in IGNORED_FOLDERS:
                continue

            if "." not in file_name:
                # possible directory, try listing it
                self._logger.debug(
                    f"{file_path} looks like a folder, trying to fetch its list"
                )
                result += self._recursive_ftp_list(ftp, file_path)
                continue

            if not any(file_name.endswith(ext) for ext in RELEVANT_EXTENSIONS):
                continue

            # fetch size
            try:
                size = ftp.size(file_path)
            except Exception as e:
                self._logger.exception(f"Error retrieving size of {file_path}: {e}")
                continue

            # fetch date
            try:
                date_response = ftp.sendcmd(f"MDTM {file_path}").replace("213 ", "")
                timestamp = datetime.strptime(date_response, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except Exception as e:
                self._logger.exception(
                    f"Error retrieving modification date of {file_path}: {e}"
                )
                continue

            result.append(
                FileInfo(
                    path=file_path.lstrip("/"),
                    size=size,
                    modified=timestamp.timestamp(),
                    permissions="",
                )
            )

        return result

    def _fetch_printer_files_from_ftp(self):
        try:
            ftp = self._client.ftp_connection()
            self._files = self._recursive_ftp_list(ftp)

        except Exception as e:
            self._logger.exception(f"Error connecting to FTP: {e}")

        finally:
            if ftp:
                ftp.close()

    def refresh_printer_files(
        self, blocking=False, timeout=10, *args, **kwargs
    ) -> None:
        if not self._client or not self._client.connected:
            return

        thread = threading.Thread(target=self._fetch_printer_files_from_ftp)
        thread.daemon = True
        thread.start()

        if blocking:
            thread.join()

    def get_printer_files(self, refresh=False, recursive=False, *args, **kwargs):
        if not self.printer_files_mounted:
            return []

        if refresh:
            self.refresh_printer_files(blocking=True)

        return [self._to_printer_file(f) for f in self._files]

    def create_printer_folder(self, target: str, *args, **kwargs) -> None:
        self._client.create_folder(target).result()

    def delete_printer_folder(
        self, target: str, recursive: bool = False, *args, **kwargs
    ):
        self._client.delete_folder(target, force=recursive).result()

    def copy_printer_folder(self, source, target, *args, **kwargs):
        self._client.copy_path(source, target).result()

    def move_printer_folder(self, source, target, *args, **kwargs):
        self._client.move_path(source, target).result()

    def upload_printer_file(
        self, path_or_file, path, upload_callback, *args, **kwargs
    ) -> str:
        def on_upload_done(future: Future) -> None:
            try:
                future.result()
                if callable(upload_callback):
                    upload_callback(done=True)
            except Exception:
                if callable(upload_callback):
                    upload_callback(failed=True)
                self._logger.exception(f"Uploading to {path} failed")

        self._client.upload_file(path_or_file, path).add_done_callback(on_upload_done)
        return path

    def download_printer_file(self, path, *args, **kwargs):
        return self._client.download_file(path)

    def delete_printer_file(self, path, *args, **kwargs):
        self._client.delete_file(path).result()

    def copy_printer_file(self, source, target, *args, **kwargs):
        self._client.copy_path(source, target).result()

    def move_printer_file(self, source, target, *args, **kwargs):
        self._client.move_path(source, target).result()

    # ~~ BambuClientListener interface

    def on_bambu_connected(self):
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
        self._listener.on_printer_files_available(True)
        self.refresh_printer_files()

    def on_bambu_disconnected(self, error: str = None):
        self._listener.on_printer_files_available(False)
        if error:
            self._error = error
            self.set_state(ConnectedPrinterState.CLOSED_WITH_ERROR, error=error)
        else:
            self.state = ConnectedPrinterState.CLOSED
        eventManager().fire(Events.DISCONNECTED)

    def on_bambu_server_info(self, server_info):
        firmware_info = FirmwareInformation(
            name="Klipper",
            data={
                "moonraker_version": server_info.get("moonraker_version", "unknown"),
                "api_version": server_info.get("api_version_string", "0.0.0"),
            },
        )
        self._listener.on_printer_firmware_info(firmware_info)

    def on_bambu_temperature_update(
        self, data: dict[str, TemperatureDataPoint]
    ) -> None:
        self._listener.on_printer_temperature_update(
            {key: (value.actual, value.target) for key, value in data.items()}
        )

    def on_bambu_gcode_log(self, *lines: str) -> None:
        self._listener.on_printer_logs(*lines)

    def on_bambu_printer_files_updated(self, files: list[FileInfo]):
        self._files = [self._to_printer_file(f) for f in files]

        self._job_cache = [
            f.path for f in self._files if f.path.startswith(".octoprint/")
        ]

        self._listener.on_printer_files_refreshed(self._files)

    def on_bambu_printer_state_changed(self, state: PrinterState) -> None:
        self._printer_state = state

        if state == PrinterState.PRINTING:
            if self.state not in (
                ConnectedPrinterState.STARTING,
                ConnectedPrinterState.PAUSING,
                ConnectedPrinterState.PAUSED,
                ConnectedPrinterState.RESUMING,
                ConnectedPrinterState.PRINTING,
            ):
                # externally triggered print job, let's see what's being printed
                def on_status(future: Future[tuple[PrintStats, SDCardStats]]) -> None:
                    try:
                        print_stats, virtual_sdcard = cast(
                            tuple[PrintStats, SDCardStats], future.result()
                        )

                        path = print_stats.filename
                        size = virtual_sdcard.file_size

                        if path is None or size is None:
                            raise ValueError("Missing path or size")

                        job = PrintJob(storage="printer", path=path, size=size)

                    except Exception:
                        self._logger.exception(
                            "Error while querying status, setting unknown job"
                        )

                        job = PrintJob(storage="printer", path="???")

                    self.set_job(job)
                    self._listener.on_printer_job_changed(job)

                    self.state = ConnectedPrinterState.PRINTING

                self._client.query_print_status().add_done_callback(on_status)

            elif self.state != ConnectedPrinterState.PRINTING:
                if self.state in (
                    ConnectedPrinterState.PAUSING,
                    ConnectedPrinterState.PAUSED,
                ):
                    self.state = ConnectedPrinterState.RESUMING
                self._evaluate_actual_status()

        elif state == PrinterState.PAUSED:
            self._evaluate_actual_status()

        elif state in (
            PrinterState.COMPLETE,
            PrinterState.CANCELLED,
            PrinterState.ERROR,
        ):
            if state == PrinterState.COMPLETE:
                self.state = ConnectedPrinterState.FINISHING
            else:
                self.state = ConnectedPrinterState.CANCELLING
            self._evaluate_actual_status()

        elif state == PrinterState.STANDBY:
            self._evaluate_actual_status()

    def on_bambu_print_progress(
        self,
        progress: float = None,
        file_position: int = None,
        elapsed_time: float = None,
        cleaned_time: float = None,
    ):
        if self._progress is None and self.current_job is not None:
            self._progress = JobProgress(
                job=self.current_job,
                progress=0.0,
                pos=0,
                elapsed=0.0,
                cleaned_elapsed=0.0,
            )

        dirty = False

        if progress is not None:
            self._progress.progress = progress
            dirty = True
        if file_position is not None:
            self._progress.pos = file_position
            dirty = True
        if elapsed_time is not None:
            self._progress.elapsed = elapsed_time
            dirty = True
        if cleaned_time is not None:
            self._progress.cleaned_elapsed = cleaned_time
            dirty = True

        if dirty:
            self._listener.on_printer_job_progress()

    def on_bambu_idle_state(self, state: IdleState):
        self._idle_state = state
        self._evaluate_actual_status()

    def on_bambu_action_command(
        self, line: str, action: str, params: str = None
    ) -> None:
        if action == "start":
            if self.get_current_job():
                self.start_print()

        elif action == "cancel":
            self.cancel_print()

        elif action == "pause":
            self.pause_print()

        elif action == "paused":
            # already handled differently
            pass

        elif action == "resume":
            self.resume_print()

        elif action == "resumed":
            # already handled differently
            pass

        elif action == "disconnect":
            self.disconnect()

        elif action in ("sd_inserted", "sd_updated"):
            self.refresh_printer_files()

        elif action == "shutdown":
            if self._settings.getBoolean(["serial", "enableShutdownActionCommand"]):
                from octoprint.server import system_command_manager

                try:
                    system_command_manager.perform_system_shutdown()
                except Exception as ex:
                    self._logger.error(f"Error executing system shutdown: {ex}")
            else:
                self._logger.warning(
                    "Received a shutdown command from the printer, but processing of this command is disabled"
                )

        action_command = action + f" {params}" if params else ""
        for name, hook in self._plugin_manager.get_hooks(
            "octoprint.comm.protocol.action"
        ).items():
            try:
                hook(self, line, action_command, name=action, params=params)
            except Exception:
                self._logger.exception(
                    f"Error while calling hook from plugin {name} with action command {action_command}",
                    extra={"plugin": name},
                )

    def on_bambu_position_update(self, position: Coordinate):
        prev = self._position
        if prev and prev.z != position.z:
            self._event_manager.fire(
                Events.Z_CHANGE, {"new": position.z, "old": prev.z}
            )
        self._position = position

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

    def _to_printer_file(self, info: FileInfo) -> PrinterFile:
        parts = info.path.split("/")
        return PrinterFile(
            path=info.path, display=parts[-1], size=info.size, date=int(info.modified)
        )
