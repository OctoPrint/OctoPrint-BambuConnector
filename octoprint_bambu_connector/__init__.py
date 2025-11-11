import octoprint.plugin
from flask_babel import gettext
from octoprint.logging.handlers import TriggeredRolloverLogHandler


class BambuRolloverLogHandler(TriggeredRolloverLogHandler):
    pass


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

        self._logging_handler = None

    # ~~ Template Plugin mixin

    def get_template_configs(self):
        return [
            {
                "type": "connection_options",
                "name": gettext("Bambu Connection"),
                "connector": "bambu",
                "template": "bambu_connector_connection_option.jinja2",
                "custom_bindings": True,
            }
        ]

    def is_template_autoescaped(self):
        return True

    # ~~ Software update hook

    def get_update_information(self):
        return {
            "bambu_connector": {
                "displayName": "Bambu Connector",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "jneilliii",
                "repo": "OctoPrint-BambuConnector",
                "current": self._plugin_version,
                "pip": "https://github.com/jneilliii/OctoPrint-BambuConnector/archive/{target_version}.zip",
            }
        }


def support_gcode_3mf_machinecode(*args, **kwargs):
    return {"machinecode": {"3mf": ("gcode.3mf",)}}


__plugin_name__ = "Bambu Connector"
__plugin_author__ = "jneilliii"
__plugin_description__ = (
    "A printer connector plugin to support communication with Bambu printers."
)
__plugin_license__ = "AGPLv3"
__plugin_pythoncompat__ = ">=3.9,<4"
__plugin_implementation__ = BambuConnectorPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    "octoprint.filemanager.extension_tree": support_gcode_3mf_machinecode,
}
