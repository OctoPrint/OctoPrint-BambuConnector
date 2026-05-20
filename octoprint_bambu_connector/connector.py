import datetime
import enum
import io
import logging
import math
import os
import pathlib
import re
import tempfile
import threading
import time
import zipfile
import zoneinfo
from typing import IO, TYPE_CHECKING, Any, Optional

import bpm
from bpm.bambutools import PlateType, ServiceState
from octoprint.events import Events, eventManager
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.storage import (
    StorageCapabilities,
    StorageThumbnail,
)
from octoprint.printer import (
    JobProgress,
    PrinterFile,
    PrinterFilesError,
    PrinterFilesMixin,
)
from octoprint.printer.connection import (
    OPERATIONAL_STATES,
    PRINTING_STATES,
    CLOSED_STATES,
    ConnectedPrinter,
    ConnectedPrinterListenerMixin,
    ConnectedPrinterState,
)
from octoprint.printer.job import PrintJob
from octoprint.printer import FirmwareInformation
from octoprint.schema import BaseModel
from octoprint.util.version import is_version_compatible, safe_get_package_version
from octoprint.util.tz import LOCAL_TZ

GCODE_STATE_LOOKUP = {
    "FAILED": ConnectedPrinterState.ERROR,
    "FINISH": ConnectedPrinterState.OPERATIONAL,
    "IDLE": ConnectedPrinterState.OPERATIONAL,
    "INIT": ConnectedPrinterState.CONNECTING,
    "OFFLINE": ConnectedPrinterState.CLOSED,
    "PAUSE": ConnectedPrinterState.PAUSED,
    "PREPARE": ConnectedPrinterState.STARTING,
    "RUNNING": ConnectedPrinterState.PRINTING,
    "UNKNOWN": ConnectedPrinterState.CLOSED,
}


IGNORED_FOLDERS = (
    "/logger/",
    "/recorder/",
    "/timelapse/",
    "/image/",
    "/ipcam/",
    "/x1plus/",
)


if TYPE_CHECKING:
    from octoprint.events import EventManager
    from octoprint.filemanager import FileManager
    from octoprint.plugin import PluginManager, PluginSettings


class ThumbnailInfo(BaseModel):
    width: int
    height: int
    size: int
    relative_path: str


class GcodeState(enum.Enum):
    FAILED = "FAILED"
    FINISH = "FINISH"
    IDLE = "IDLE"
    INIT = "INIT"
    OFFLINE = "OFFLINE"
    PAUSE = "PAUSE"
    PREPARE = "PREPARE"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def for_value(cls, value: str) -> "GcodeState":
        for state in cls:
            if state.value == value:
                return state
        return GcodeState.UNKNOWN


OPERATIONAL_GCODE_STATES = (
    GcodeState.IDLE,
    GcodeState.FAILED,
    GcodeState.FINISH,
    GcodeState.PAUSE,
    GcodeState.PREPARE,
    GcodeState.RUNNING,
)

PRINTING_GCODE_STATES = (
    GcodeState.PAUSE,
    GcodeState.PREPARE,
    GcodeState.RUNNING,
)

INITIAL_STATE_TIMEOUT = 30


class JobStage(enum.Enum):
    PRINTING = 0
    AUTO_BED_LEVELING = 1
    HEATBED_PREHEATING = 2
    SWEEPING_XY_MECH_MODE = 3
    CHANGING_FILAMENT = 4
    M400_PAUSE = 5
    RUNOUT_PAUSE = 5
    HEATING_HOTEND = 7
    CALIBRATING_EXTRUSION = 8
    SCANNING_BED_SURFACE = 9
    INSPECTING_FIRST_LAYER = 10
    IDENTIFYING_BUILD_PLATE_TYPE = 11
    CALIBRATING_MICRO_LIDAR = 12
    HOMING_TOOLHEAD = 13
    CLEANING_NOZZLE_TIP = 14
    CHECKING_EXTRUDER_TEMPERATURE = 15
    USER_PAUSE = 16
    FRONT_COVER_ERROR = 17
    CALIBRATING_MICRO_LIDAR_2 = 18
    CALIBRATING_EXTRUSION_2 = 19
    NOZZLE_TEMPERATURE_ERROR = 20
    BED_TEMPERATURE_ERROR = 21
    FILAMENT_UNLOADING = 22
    SKIPPED_STEPS_ERROR = 23
    FILAMENT_LOADING = 24
    CALIBRATING_MOTOR_NOISE = 25
    AMS_LOST_ERROR = 26
    HEAT_BREAK_FAN_ERROR = 27
    CHAMBER_TEMPERATURE_ERROR = 28
    COOLING_CHAMBER = 29
    GCODE_PAUSE = 30
    MOTOR_NOISE_SHOWOFF = 31
    NOZZLE_FILAMENT_COVERED_ERROR = 32
    CUTTER_ERROR = 33
    FIRST_LAYER_ERROR = 34
    NOZZLE_CLOG_ERROR = 35

    FINISHING = 255

    UNKNOWN = -1

    @classmethod
    def for_value(cls, value: int) -> "JobStage":
        for state in cls:
            if state.value == value:
                return state
        return JobStage.UNKNOWN


STARTING_JOB_STAGES = (
    JobStage.AUTO_BED_LEVELING,
    JobStage.HEATBED_PREHEATING,
    JobStage.SWEEPING_XY_MECH_MODE,
    JobStage.HEATING_HOTEND,
    JobStage.SCANNING_BED_SURFACE,
    JobStage.IDENTIFYING_BUILD_PLATE_TYPE,
    JobStage.CALIBRATING_EXTRUSION,
    JobStage.CALIBRATING_EXTRUSION_2,
    JobStage.CALIBRATING_MICRO_LIDAR,
    JobStage.CALIBRATING_MICRO_LIDAR_2,
    JobStage.CALIBRATING_MOTOR_NOISE,
    JobStage.CLEANING_NOZZLE_TIP,
    JobStage.CHECKING_EXTRUDER_TEMPERATURE,
)

FINISHING_JOB_STAGES = (JobStage.FINISHING,)


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
        move_file=True,
        add_folder=True,
        remove_folder=True,
        copy_folder=False,
        move_folder=False,
        thumbnails=True,
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
    _bpm_cache_folder: str = None
    _thumbs_cache_folder: str = None
    # /injected

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logger = logging.getLogger(__name__)

        self._host = kwargs.get("host")
        self._serial = kwargs.get("serial")
        self._access_code = kwargs.get("access_code")

        self._client = None

        self._state = ConnectedPrinterState.CLOSED
        self._state_context: Optional[tuple[ConnectedPrinterState, str]] = None
        self._connection_state: ServiceState = ServiceState.NO_STATE
        self._gcode_state = GcodeState.UNKNOWN
        self._job_stage = JobStage.UNKNOWN

        self._error = None
        self._disconnecting = False
        self._disconnect_thread = None

        self._progress: JobProgress = None
        self._old_progress: int = None
        self._old_time_remaining: int = None
        self._last_printer_job: tuple[str, int] = None

        self._files: list[PrinterFile] = []

        self._ptz = None

        timezone_str = self._plugin_settings.get(["printer_timezone"])
        if timezone_str is not None and timezone_str != "":
            try:
                self._ptz = zoneinfo.ZoneInfo(timezone_str)
            except Exception:
                self._logger.exception(
                    f"Cannot load configured printer timezone {timezone_str}, falling back to server timezone"
                )

        self._bpm_version = safe_get_package_version("bambu-printer-manager")

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
            and state in OPERATIONAL_STATES
        ):
            self._event_bus.fire(
                Events.CONNECTED,
                {
                    "connector": self.connector,
                    "host": self._host,
                    "serial": self._serial,
                    "access_code": self._access_code is not None,
                },
            )

        if state in OPERATIONAL_STATES:
            if old_state not in OPERATIONAL_STATES:
                # we just connected
                self.refresh_printer_files(blocking=True)
                self._listener.on_printer_files_available(True)

            if state in PRINTING_STATES:
                if old_state not in PRINTING_STATES and not self.current_job:
                    # we went from not printing to printing without having a job
                    # -> this was triggered by the printer!
                    self.set_job(
                        PrintJob(
                            storage=FileDestinations.PRINTER, path="???", display="???"
                        )
                    )
                    self._listener.on_printer_job_changed(self.current_job)
                self._listener.on_printer_job_started()

            elif old_state in PRINTING_STATES:
                # we went from printing to not printing, so the current job is done
                # one way or the other

                if self._gcode_state == GcodeState.FINISH:
                    # job completed
                    if self._progress is not None:
                        self._progress.progress = 1.0
                    self._listener.on_printer_job_done()

                elif self._gcode_state == GcodeState.FAILED:
                    # job failed
                    self._listener.on_printer_job_cancelled()

                else:
                    # TODO no clue what best to do here...
                    pass

        else:
            if old_state in OPERATIONAL_STATES:
                self._listener.on_printer_files_available(False)

        super().set_state(state, error=error)

        message = f"State changed from {old_state.name} to {self.state.name}"
        self._logger.info(message)
        self._listener.on_printer_logs(message)

    def get_state_string(self, state: ConnectedPrinterState = None):
        # TODO this requires state updates to work, but those are prevented by the state itself staying the same
        if state is None:
            state = self.state

        context = self._state_context
        if context and context[0] == state and context[1]:
            return f"{state.value} ({context[1]})"

        return state.value

    @property
    def job_progress(self) -> JobProgress:
        return self._progress

    def connect(self, *args, **kwargs):
        from . import BambuFormatter, BambuRolloverLogHandler

        if (
            self._client is not None
            or self._host == ""
            or self._serial == ""
            or self._access_code == ""
        ):
            return

        BambuRolloverLogHandler.arm_rollover()
        BambuFormatter.masked = [self._host, self._serial, self._access_code]

        eventManager().fire(Events.CONNECTING)
        self.set_state(ConnectedPrinterState.CONNECTING)

        try:
            self._logger.info("Connecting to Bambu")

            bpm_cache_path = pathlib.Path(self._bpm_cache_folder)

            config = bpm.bambuconfig.BambuConfig(
                hostname=self._host,
                access_code=self._access_code,
                serial_number=self._serial,
                bpm_cache_path=bpm_cache_path,
            )
            printer = bpm.bambuprinter.BambuPrinter(config=config)

            printer.on_update = self._on_bpm_update

            printer.start_session()

            start = time.monotonic()
            while printer.service_state == ServiceState.NO_STATE:
                # await *some* state
                time.sleep(0.1)
                if time.monotonic() > start + INITIAL_STATE_TIMEOUT:
                    # if there's still no state, something has probably gone terribly wrong
                    break

            if printer.service_state != ServiceState.CONNECTED:
                internal_exception = printer.internalException
                msg = "Connection failed"
                if internal_exception:
                    raise RuntimeError(
                        f"{msg}: {internal_exception!s}"
                    ) from internal_exception
                else:
                    raise RuntimeError(msg)
        except Exception as exc:
            self._logger.exception(
                "Error while connecting to bambu printer through bpm"
            )
            self.set_state(ConnectedPrinterState.CLOSED_WITH_ERROR, error=str(exc))
            return False

        self._client = printer
        return True

    def disconnect(self, *args, **kwargs):
        if self._client is None:
            return

        if self._disconnecting:
            return
        self._disconnecting = True

        if is_version_compatible(self._bpm_version, "<=1.0.2"):
            # workaround until synman/bambu-printer-manager#54 is merged
            self._client._client.disconnect()
        self._client.quit()
        self.set_state(
            ConnectedPrinterState.CLOSED
        )  # should already have been set through self._client.quit(), but better safe than sorry

        # clean up references
        self._client.on_update = None
        self._client = None

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

    def create_job(self, path: str, owner: str = None, params: dict = None) -> PrintJob:
        plate = 1
        if params:
            plate = params.pop("plate_number", plate)

        job = super().create_job(path, owner=owner, params=params)
        job.plate = plate

        return job

    def start_print(
        self, pos=None, user=None, tags=None, params: dict = None, *args, **kwargs
    ):
        if (
            not self.current_job
            or not self.current_job.storage == FileDestinations.PRINTER
        ):
            return

        path = os.path.join("/", self.current_job.path)

        if params is None:
            params = {}

        job_params = self.current_job.params
        if job_params is None:
            job_params = {}

        def fetch_param(param: str, converter: callable = None) -> Any:
            value = params.get(
                param,
                job_params.get(
                    param, self._plugin_settings.get(["default_job_params", param])
                ),
            )

            if converter:
                return converter(value)
            return value

        use_ams = fetch_param("use_ams", converter=bool)
        perform_bed_leveling = fetch_param("perform_bed_leveling", converter=bool)
        perform_flow_cali = fetch_param("perform_flow_cali", converter=bool)
        enable_timelapse = fetch_param("enable_timelapse", converter=bool)
        self.current_job.plate = fetch_param("plate_number", converter=int)

        self.set_state(ConnectedPrinterState.STARTING)

        # TODO: deal with ams_mapping, for now will default to what is set in sliced file
        self._client.print_3mf_file(
            name=path,
            plate=self.current_job.plate,
            bed=PlateType.AUTO,  # Always assume the sliced gcode file has this set correctly
            use_ams=use_ams,
            ams_mapping="",
            bedlevel=perform_bed_leveling,
            flow=perform_flow_cali,
            timelapse=enable_timelapse,
        )

    def pause_print(self, tags=None, params: dict = None, *args, **kwargs):
        if self._client is None:
            return
        self._client.pause_printing()

    def resume_print(self, tags=None, params: dict = None, *args, **kwargs):
        if self._client is None:
            return
        self._client.resume_printing()

    def cancel_print(self, tags=None, params: dict = None, *args, **kwargs):
        if self._client is None:
            return
        self._client.stop_printing()

    # ~~ PrinterFilesMixin

    @property
    def printer_files_mounted(self) -> bool:
        return self._client is not None

    def _update_file_cache(self, files: dict):
        self._files = self._to_printer_files(files.get("children", []))
        self._listener.on_printer_files_refreshed(self._files)

    def refresh_printer_files(
        self, blocking=False, timeout=30, *args, **kwargs
    ) -> None:
        if not self._client or not self._client.service_state == ServiceState.CONNECTED:
            self._files = []
            return

        def perform_refresh():
            files = self._client.get_sdcard_contents()
            self._update_file_cache(files)

        thread = threading.Thread(target=perform_refresh)
        thread.daemon = True
        thread.start()

        if blocking:
            thread.join(timeout=timeout)

    def get_printer_files(self, refresh=False, recursive=False, *args, **kwargs):
        if not self.printer_files_mounted:
            return []

        if not self._files or refresh:
            self.refresh_printer_files(blocking=True)

        return self._files

    def create_printer_folder(self, target: str, *args, **kwargs) -> None:
        try:
            files = self._client.make_sdcard_directory(target)
            self._update_file_cache(files)
            return target
        except Exception as exc:
            raise PrinterFilesError("Folder creation failed") from exc

    def delete_printer_folder(
        self, target: str, recursive: bool = False, *args, **kwargs
    ):
        path = os.path.join("/", target)
        try:
            files = self._client.delete_sdcard_folder(path)
            self._update_file_cache(files)
        except Exception as exc:
            message = f"There was an error deleting folder {path}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc

    def copy_printer_folder(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def move_printer_folder(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def upload_printer_file(
        self, source, target, progress_callback: callable = None, *args, **kwargs
    ) -> str:
        try:
            path = os.path.join("/", target)

            if isinstance(source, str):
                # this is a path, we can use this right away
                files = self._client.upload_sdcard_file(source, path)
            else:
                # this is a stream, we need to dump it into a temporary file before we can proceed
                with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp:
                    try:
                        temp.write(source.read())
                        temp.close()
                        files = self._client.upload_sdcard_file(temp.name, path)
                    finally:
                        os.remove(temp.name)

            self._update_file_cache(files)
            progress_callback(done=True)
            return path
        except Exception as exc:
            progress_callback(failed=True)
            raise PrinterFilesError(f"There was an error uploading to {path}") from exc

    def download_printer_file(self, path, *args, **kwargs):
        try:
            src = os.path.join("/", path)

            with tempfile.NamedTemporaryFile(delete=False) as temp:
                # delete_on_close=False, delete=True would be better, but delete_on_close is only available from Python 3.12 onward
                try:
                    temp.close()

                    start = time.monotonic()
                    self._client.download_sdcard_file(src, temp.name)
                    duration = time.monotonic() - start
                    if self._logger.isEnabledFor(logging.DEBUG):
                        size = os.stat(temp.name).st_size
                        speed = size / duration / 1024.0  # KB/s
                        self._logger.debug(
                            f"Fetched {path} in {duration:0.2f}s at {speed:0.2f}KB/s"
                        )

                    with open(temp.name, "rb") as f:
                        file_object = io.BytesIO(f.read())
                    return file_object
                finally:
                    os.remove(temp.name)
        except Exception as exc:
            message = f"There was an error downloading file {path}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc

    def delete_printer_file(self, path, *args, **kwargs):
        try:
            path = os.path.join("/", path)
            files = self._client.delete_sdcard_file(path)
            self._update_file_cache(files)
        except Exception as exc:
            message = f"There was an error deleting file {path}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc

    def copy_printer_file(self, source, target, *args, **kwargs):
        raise NotImplementedError()

    def move_printer_file(self, source, target, *args, **kwargs):
        try:
            files = self._client.rename_sdcard_file(source, target)
            self._update_file_cache(files)
            return target
        except Exception as exc:
            message = f"There was an error moving file {source}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc

    def has_thumbnail(self, path, *args, **kwargs):
        return self._thumbs_cache_folder and path.endswith(".3mf")

    def get_thumbnail(
        self, path, platehint=None, sizehint=None, *args, **kwargs
    ) -> Optional[StorageThumbnail]:
        return self._to_storage_thumbnail(path)

    def download_thumbnail(
        self, path, platehint=None, sizehint=None, *args, **kwargs
    ) -> Optional[tuple[StorageThumbnail, IO]]:
        thumbnails_path = self._thumbnails_path(path)
        if not os.path.exists(thumbnails_path) or len(os.listdir(thumbnails_path)) == 0:
            self._fetch_thumbnails(path)

        if not os.path.isdir(thumbnails_path):
            return None

        try:
            # touch the path so we know it has recently been accessed
            os.utime(thumbnails_path, None)
        except OSError:
            pass

        if platehint is None:
            platehint = 1

        try:
            thumbnail_file = f"plate_{platehint}.png"
            thumbnail_path = os.path.join(
                thumbnails_path,
                thumbnail_file,
            )
            if os.path.exists(thumbnail_path):
                info = self._to_storage_thumbnail(thumbnail_path)
                return info, open(thumbnail_path, mode="rb")
        except Exception as exc:
            message = f"There was an error downloading the thumbnail for {path}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc
        return None

    def refresh_thumbnails(self, path, force=False, recursive=False):
        if self._is_dir(path):
            # directory
            prefix = f"{path}/"
            children = [item for item in self._files if item.path.startswith(prefix)]
            paths = []
            for child in children:
                if not recursive and "/" in child.path[len(prefix) :]:
                    continue
                if not child.path.endswith(".3mf"):
                    continue
                paths.append(child.path)

        else:
            # single file
            if not path.endswith(".3mf"):
                return

            paths = [path]

        if not len(paths):
            return

        for p in paths:
            if force or not self._thumbnails_cached(p):
                try:
                    self._fetch_thumbnails(p)
                except PrinterFilesError:
                    pass

    def _to_storage_thumbnail(self, path: str) -> StorageThumbnail:
        name = path
        if "/" in path:
            name = path.rsplit("/", maxsplit=1)[1]
        stat = os.stat(path)
        mime = "image/png"

        return StorageThumbnail(
            name=name,
            printable=path,
            sizehint="",
            mime=mime,
            size=stat.st_size,
            last_modified=int(stat.st_mtime),
        )

    def _fetch_thumbnails(self, path: str):
        if not path.endswith(".3mf"):
            return

        thumbnails_path = self._thumbnails_path(path)
        try:
            file = self.download_printer_file(path)
            with zipfile.ZipFile(file, "r") as zipObj:
                for zipFileName in zipObj.namelist():
                    filename_match = re.match(
                        r"Metadata/(?P<filename>plate_\d+(_small)?.png)",
                        zipFileName,
                    )
                    if filename_match:
                        filename = filename_match.group("filename")

                        zipInfo = zipObj.getinfo(zipFileName)
                        zipInfo.filename = filename
                        zipObj.extract(zipInfo, thumbnails_path)

                        self._logger.debug(f"Cached thumbnail {filename} for {path}")
        except Exception as exc:
            message = f"There was an error extracting thumbnail for {path}"
            self._logger.exception(message)
            raise PrinterFilesError(message) from exc

    def _thumbnails_path(self, path: str):
        return os.path.join(self._thumbs_cache_folder, path)

    def _thumbnails_cached(self, path: str):
        thumbnails_path = self._thumbnails_path(path)
        return os.path.exists(thumbnails_path) and len(os.listdir(thumbnails_path)) > 0

    def _is_dir(self, path: str) -> bool:
        if path.endswith("/"):
            return True
        return any(item.path.startswith(f"{path}/") for item in self._files)

    # ~~ BPM callback

    def _on_bpm_update(self, printer: bpm.bambuprinter.BambuPrinter) -> None:
        if printer != self._client:
            return

        try:
            self._update_state_from_state(
                printer
            )  # eval state before job, so current state is known in job eval
            self._update_job_from_state(printer)
            self._update_progress_from_state(printer)
            self._update_temperatures_from_state(printer)
        except Exception:
            self._logger.exception("Error while processing BPM update")

    def _update_job_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        if self.state not in OPERATIONAL_STATES:
            return

        # if printer.current_3mf_file:
        #     current_path = printer.current_3mf_file
        if printer.active_job_info.subtask_name and (
            any(
                f"{printer.active_job_info.subtask_name}" in file.path
                for file in self._files
            )
            or any(
                f"{printer.active_job_info.subtask_name}.gcode.3mf" in file.path
                for file in self._files
            )
        ):
            if printer.active_job_info.subtask_name.endswith(".gcode.3mf"):
                current_path = printer.active_job_info.subtask_name
            else:
                current_path = f"{printer.active_job_info.subtask_name}.gcode.3mf"
        elif printer.active_job_info.gcode_file:
            current_path = printer.active_job_info.gcode_file
        else:
            return

        plate = printer.active_job_info.plate_num
        if plate < 0:
            plate_match = re.match(
                r".+plate_(?P<plate>\d+).gcode",
                printer.active_job_info.gcode_file,
            )
            if plate_match:
                plate = int(plate_match.group("plate"))
            else:
                plate = 1

        if (
            self._last_printer_job == (current_path, plate)
            and self.state not in PRINTING_STATES
        ):
            return
        self._last_printer_job = (current_path, plate)

        if (
            self.current_job
            and (
                self.current_job.path == current_path
                or self.current_job.storage != FileDestinations.PRINTER
            )
            and (self.current_job.plate == plate)
        ):
            return

        display = current_path.rsplit("/")[-1]

        size = 0
        date = None
        if self._files:
            for f in self._files:
                if f.path == current_path:
                    size = f.size
                    date = f.date
                    break

        job = PrintJob(
            storage=FileDestinations.PRINTER,
            path=current_path,
            display=display,
            size=size,
            date=date,
            plate=plate,
        )

        self.set_job(job)
        self._listener.on_printer_job_changed(job)

    def _update_state_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        old_stage = self._job_stage

        self._connection_state = printer.service_state
        self._gcode_state = GcodeState.for_value(printer.printer_state.gcode_state)
        self._job_stage = JobStage.for_value(printer.active_job_info.stage_id)

        self._logger.debug(
            f"STATE UPDATE -- printer_state = {self._connection_state} - gcode_state = {self._gcode_state} - current_stage = {self._job_stage} ({printer.active_job_info.stage_id})"
        )

        if self._job_stage != old_stage and printer.active_job_info.stage_name:
            self._to_terminal(f"Current stage: {printer.active_job_info.stage_name}")
            eventManager().fire(
                Events.CHART_MARKED,
                {
                    "type": "bambu_connector_job_stage",
                    "label": printer.active_job_info.stage_name,
                },
            )

        new_state = None
        error = None

        if self._connection_state == ServiceState.CONNECTED:
            if self._gcode_state in PRINTING_GCODE_STATES:
                if self._gcode_state == GcodeState.PREPARE:
                    new_state = ConnectedPrinterState.STARTING

                elif self._gcode_state == GcodeState.RUNNING:
                    if self._job_stage == JobStage.PRINTING:
                        new_state = ConnectedPrinterState.PRINTING
                    elif (
                        self._job_stage in FINISHING_JOB_STAGES
                        and self.state == ConnectedPrinterState.PRINTING
                    ):
                        new_state = ConnectedPrinterState.FINISHING
                    elif self.state not in PRINTING_STATES:
                        new_state = ConnectedPrinterState.STARTING

                elif self._gcode_state == GcodeState.PAUSE:
                    new_state = ConnectedPrinterState.PAUSED

            elif self._gcode_state in OPERATIONAL_GCODE_STATES or (
                self.state == ConnectedPrinterState.CONNECTING
                and self._gcode_state == GcodeState.UNKNOWN
            ):
                new_state = ConnectedPrinterState.OPERATIONAL

            elif self._gcode_state == GcodeState.INIT:
                new_state = ConnectedPrinterState.CONNECTING

            elif self._gcode_state == GcodeState.OFFLINE:
                new_state = ConnectedPrinterState.CLOSED

        elif self._connection_state == ServiceState.DISCONNECTED:
            if not self._disconnecting and not self._disconnect_thread:
                message = "Lost connection to printer"
                self._logger.warning(message)
                self._listener.on_printer_logs(message)
                self._disconnect_thread = threading.Thread(
                    target=self.disconnect
                ).start()  # decouple this call from the status update thread or bpm will run into an issue on thread join in `quit`

        elif self._connection_state == ServiceState.QUIT:
            if self.state not in (
                ConnectedPrinterState.CLOSED,
                ConnectedPrinterState.CLOSED_WITH_ERROR,
            ):
                internal_error = printer.internalException
                if internal_error:
                    new_state = ConnectedPrinterState.CLOSED_WITH_ERROR
                    error = str(internal_error)
                else:
                    new_state = ConnectedPrinterState.CLOSED

        if new_state:
            self._state_context = (new_state, printer.active_job_info.stage_name)
            self.set_state(new_state, error=error)

            if new_state in CLOSED_STATES:
                self.firmware_info = None
            elif not self.firmware_info:
                self.firmware_info = FirmwareInformation(
                    name="Bambu",
                    data={
                        "model": printer.config.printer_model.name,
                        "version": printer.config.firmware_version,
                        "ams_firmware_version": printer.config.ams_firmware_version,
                    },
                )

    def _update_progress_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        if self.current_job is None:
            return

        if self.state not in PRINTING_STATES:
            return

        if self._progress is None:
            self._progress = JobProgress(
                job=self.current_job,
                progress=0.0,
                pos=0,
                elapsed=0.0,
                cleaned_elapsed=0.0,
            )

        progress = printer.active_job_info.print_percentage
        if self.state == ConnectedPrinterState.STARTING and progress == 100:
            # left over from a previous print of the same file
            progress = 0

        if self.state in PRINTING_STATES and (
            self._old_progress != progress
            or self._old_time_remaining != printer.active_job_info.remaining_minutes
        ):
            self._to_terminal(
                f"Progress: {progress}%, time remaining: {self._format_minutes(printer.active_job_info.remaining_minutes)}"
            )

        self._old_progress = progress
        self._old_time_remaining = printer.active_job_info.remaining_minutes

        self._progress.progress = float(progress) / 100.0
        self._progress.left_estimate = printer.active_job_info.remaining_minutes * 60.0
        if self.current_job and self.current_job.size:
            self._progress.pos = int(self.current_job.size * self._progress.progress)
        self._listener.on_printer_job_progress()

    def _update_temperatures_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        self._listener.on_printer_temperature_update(
            {
                "tool0": (
                    printer.printer_state.active_nozzle_temp,
                    printer.printer_state.active_nozzle_temp_target,
                ),
                "bed": (
                    printer.printer_state.climate.bed_temp,
                    printer.printer_state.climate.bed_temp_target,
                ),
                "chamber": (
                    printer.printer_state.climate.chamber_temp,
                    printer.printer_state.climate.chamber_temp_target,
                ),
            }
        )

    ##~~ helpers

    def _to_terminal(self, message: str, prefix: str = "<<<"):
        self._listener.on_printer_logs(f"{prefix} {message}")

    def _format_minutes(self, minutes: int) -> str:
        hours = math.floor(float(minutes) / 60.0)
        mins = minutes - hours * 60
        return f"{hours}h:{mins}m"

    def _to_printer_files(self, nodes: list[dict[str, Any]]) -> list[PrinterFile]:
        result = []
        for node in nodes:
            if node["id"] in IGNORED_FOLDERS:
                continue

            timestamp = int(node.get("timestamp", 0))
            if timestamp > 0:
                if self._ptz:
                    tz = self._ptz
                else:
                    tz = LOCAL_TZ
                date = datetime.datetime.fromtimestamp(timestamp).replace(tzinfo=tz)
            else:
                date = None

            path = node["id"][1:]  # strip leading /

            if "children" in node:
                # folder
                if len(node["children"]) == 0:
                    # empty folder
                    result.append(
                        PrinterFile(
                            path=path,
                            display=node["name"],
                            size=node.get("size", 0),
                            date=date,
                        )
                    )
                else:
                    # folder contains entries
                    result += self._to_printer_files(node["children"])

            else:
                # single file
                result.append(
                    PrinterFile(
                        path=path,
                        display=node["name"],
                        size=node.get("size", 0),
                        date=date,
                        thumbnails=[""] if self.has_thumbnail(path) else [],
                    )
                )

        return result
