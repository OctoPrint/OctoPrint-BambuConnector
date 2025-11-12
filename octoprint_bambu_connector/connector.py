import enum
import logging
import threading
from typing import TYPE_CHECKING, Any

from octoprint.events import Events, eventManager
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.storage import StorageCapabilities
from octoprint.printer import JobProgress, PrinterFile, PrinterFilesMixin
from octoprint.printer.connection import (
    OPERATIONAL_STATES,
    PRINTING_STATES,
    ConnectedPrinter,
    ConnectedPrinterListenerMixin,
    ConnectedPrinterState,
)
from octoprint.printer.job import PrintJob

from .vendor import bpm

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


RELEVANT_EXTENSIONS = (".gcode", ".gco", ".gcode.3mf")
IGNORED_FOLDERS = ("/logger", "/recorder", "/timelapse", "/image", "/ipcam")
MODELS_SDCARD_MOUNT = ()


if TYPE_CHECKING:
    from octoprint.events import EventManager
    from octoprint.filemanager import FileManager
    from octoprint.plugin import PluginManager, PluginSettings


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
        self._connection_state: bpm.bambuprinter.PrinterState = (
            bpm.bambuprinter.PrinterState.NO_STATE
        )
        self._gcode_state = GcodeState.UNKNOWN
        self._job_stage = JobStage.UNKNOWN

        self._error = None

        self._progress: JobProgress = None

        self._files: list[PrinterFile] = []

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
                    "connector": self.name,
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
                    self.set_job(PrintJob(storage=FileDestinations.PRINTER, path="???"))
                    self._listener.on_printer_job_changed(self.current_job)

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
        if self._client is None:
            return
        self._client.pause_printing()

    def resume_print(self, tags=None, *args, **kwargs):
        if self._client is None:
            return
        self._client.resume_printing()

    def cancel_print(self, tags=None, *args, **kwargs):
        if self._client is None:
            return
        self._client.stop_printing()

    # ~~ PrinterFilesMixin

    @property
    def printer_files_mounted(self) -> bool:
        return self._client is not None

    def refresh_printer_files(
        self, blocking=False, timeout=30, *args, **kwargs
    ) -> None:
        if (
            not self._client
            or not self._client.state == bpm.bambuprinter.PrinterState.CONNECTED
        ):
            self._files = []
            return

        def perform_refresh():
            files = self._client.get_sdcard_contents()
            self._files = self._to_printer_files(files.get("children", []))
            self._listener.on_printer_files_refreshed(self._files)

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

    def _on_bpm_update(self, printer: bpm.bambuprinter.BambuPrinter) -> None:
        if printer != self._client:
            return

        try:
            self._update_job_from_state(printer)
            self._update_state_from_state(printer)
            self._update_progress_from_state(printer)
            self._update_temperatures_from_state(printer)
        except Exception:
            self._logger.exception("Error while processing BPM update")

    def _update_job_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        if self.state not in OPERATIONAL_STATES:
            return

        current_path = printer.subtask_name
        if not current_path:
            return

        if self.current_job and (
            self.current_job.path == current_path
            or self.current_job.storage != FileDestinations.PRINTER
        ):
            return

        display = current_path.rsplit("/")[-1]

        size = 0
        if self._files:
            for f in self._files:
                if f.path == current_path:
                    size = f.size
                    break

        job = PrintJob(
            storage=FileDestinations.PRINTER,
            path=current_path,
            display=display,
            size=size,
        )

        self.set_job(job)
        self._listener.on_printer_job_changed(job)

    def _update_state_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        self._connection_state = printer.state
        self._gcode_state = GcodeState.for_value(printer.gcode_state)
        self._current_stage = JobStage.for_value(printer.current_stage)

        self._logger.debug(
            f"STATE UPDATE -- printer_state = {self._connection_state} - gcode_state = {self._gcode_state} - current_stage = {self._current_stage} ({printer.current_stage})"
        )

        new_state = None

        if self._connection_state == bpm.bambuprinter.PrinterState.CONNECTED:
            if self._gcode_state in PRINTING_GCODE_STATES:
                if self._gcode_state == GcodeState.PREPARE:
                    new_state = ConnectedPrinterState.STARTING

                elif self._gcode_state == GcodeState.RUNNING:
                    if self._current_stage == JobStage.PRINTING:
                        new_state = ConnectedPrinterState.PRINTING
                    elif (
                        self._current_stage in FINISHING_JOB_STAGES
                        and self.state in PRINTING_STATES
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

        else:
            new_state = ConnectedPrinterState.CLOSED

        if new_state:
            self.set_state(new_state)

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

        self._progress.progress = float(printer.percent_complete) / 100.0
        self._progress.left_estimate = printer.time_remaining * 60.0
        if self.current_job and self.current_job.size:
            self._progress.pos = self.current_job.size * self._progress.progress
        self._listener.on_printer_job_progress()

    def _update_temperatures_from_state(self, printer: bpm.bambuprinter.BambuPrinter):
        self._listener.on_printer_temperature_update(
            {
                "tool0": (printer.tool_temp, printer.tool_temp_target),
                "bed": (printer.bed_temp, printer.bed_temp_target),
                "chamber": (printer.chamber_temp, printer.chamber_temp_target),
            }
        )

    ##~~ helpers

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
