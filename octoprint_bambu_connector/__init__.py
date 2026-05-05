import logging
import os

import octoprint.plugin
from flask_babel import gettext
from octoprint.logging.handlers import TriggeredRolloverLogHandler
from octoprint.util.tz import LOCAL_TZ

DEFAULT_PRINTER_TIMEZONE = "Asia/Shanghai"


class BambuRolloverLogHandler(TriggeredRolloverLogHandler):
    pass


class BambuFormatter(logging.Formatter):
    masked: list[str] = []
    mask: str = "*****"

    @classmethod
    def set_masked(cls, masked: list[str]):
        cls.masked = masked

    @classmethod
    def set_mask(cls, mask: str):
        cls.mask = mask

    def format(self, record):
        text = super().format(record)
        for m in self.masked:
            text = text.replace(m, self.mask)
        return text


class BambuConnectorPlugin(
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.StartupPlugin,
):
    def initialize(self):
        from .connector import ConnectedBambuPrinter

        # inject properties into connector class
        ConnectedBambuPrinter._event_bus = self._event_bus
        ConnectedBambuPrinter._file_manager = self._file_manager
        ConnectedBambuPrinter._plugin_manager = self._plugin_manager
        ConnectedBambuPrinter._plugin_settings = self._settings
        ConnectedBambuPrinter._bpm_cache_folder = os.path.join(
            self.get_plugin_data_folder(), "bpm_cache"
        )
        ConnectedBambuPrinter._thumbs_cache_folder = os.path.join(
            self.get_plugin_data_folder(), "thumbs"
        )

    def on_startup(self, host, port):
        self._configure_bpm_logging()

    def _configure_bpm_logging(self):
        handler = BambuRolloverLogHandler(
            self._settings.get_plugin_logfile_path(postfix="bpm"),
            encoding="utf-8",
            backupCount=3,
            delay=True,
        )
        handler.setFormatter(BambuFormatter("%(asctime)s %(message)s"))
        handler.setLevel(logging.DEBUG)

        logger = logging.getLogger("bambu_printer_manager")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    # ~~ Template Plugin mixin

    def get_template_configs(self):
        return [
            {
                "type": "connection_options",
                "name": gettext("Bambu Connection"),
                "connector": "bambu",
                "template": "bambu_connector_connection_option.jinja2",
                "custom_bindings": True,
            },
            {
                "type": "settings",
                "name": gettext("Bambu Connection"),
                "template": "bambu_connector_settings.jinja2",
                "custom_bindings": True,
            },
            {
                "type": "generic",
                "template": "bambu_connector.jinja2",
                "custom_bindings": True,
            },
        ]

    def is_template_autoescaped(self):
        return True

    def get_template_vars(self):
        import zoneinfo

        return {
            "timezones": [
                x
                for x in sorted(zoneinfo.available_timezones())
                if x != DEFAULT_PRINTER_TIMEZONE
            ],
            "server_timezone": LOCAL_TZ.tzname(None),
            "default_printer_timezone": DEFAULT_PRINTER_TIMEZONE,
        }

    # ~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return {
            "printer_timezone": None,
            "default_job_params": {
                "perform_bed_leveling": True,
                "perform_flow_cali": False,
                "enable_timelapse": False,
                "use_ams": True,
                "plate_number": 1,
            },
        }

    # ~~ Software update hook

    def get_update_information(self):
        return {
            "bambu_connector": {
                "displayName": "Bambu Connector",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "OctoPrint",
                "repo": "OctoPrint-BambuConnector",
                "current": self._plugin_version,
                "pip": "https://github.com/OctoPrint/OctoPrint-BambuConnector/archive/{target_version}.zip",
            }
        }


def support_gcode_3mf_machinecode(*args, **kwargs):
    return {"machinecode": {"3mf": ("3mf",)}}


__plugin_name__ = "Bambu Connector"
__plugin_author__ = "jneilliii, Gina Häußge"
__plugin_description__ = (
    "A printer connector plugin to support communication with Bambu printers."
)
__plugin_license__ = "AGPLv3"
__plugin_pythoncompat__ = ">=3.11,<4"
__plugin_implementation__ = BambuConnectorPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    "octoprint.filemanager.extension_tree": support_gcode_3mf_machinecode,
}
