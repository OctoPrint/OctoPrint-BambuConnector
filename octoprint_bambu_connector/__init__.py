# coding=utf-8
from __future__ import absolute_import
import octoprint.plugin
import logging
from flask_babel import gettext
import octoprint.plugin
from octoprint.logging.handlers import TriggeredRolloverLogHandler


class BambuRolloverLogHandler(TriggeredRolloverLogHandler):
    pass


class BambuConnectorPlugin(
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.StartupPlugin,
):

    def __init__(self):
        super().__init__()
        self._logging_handler = None

    def initialize(self):
        self._logging_handler = None
        from .connector import ConnectedBambuPrinter

    def on_startup(self, host, port):
        self._configure_logging()

    def _configure_logging(self):
        handler = BambuRolloverLogHandler(
            self._settings.get_plugin_logfile_path(postfix="mqtt"),
            encoding="utf-8",
            backupCount=3,
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        handler.setLevel(logging.DEBUG)

        logger = logging.getLogger(
            "octoprint.plugins.bambu_connector.mqtt.console"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = True

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


__plugin_name__ = "Bambu Connector"
__plugin_author__ = "jneilliii"
__plugin_description__ = "A printer connector plugin to support communication with Bambu printers."
__plugin_license__ = "AGPLv3"
__plugin_pythoncompat__ = ">=3.9,<4"
__plugin_implementation__ = BambuConnectorPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
}

