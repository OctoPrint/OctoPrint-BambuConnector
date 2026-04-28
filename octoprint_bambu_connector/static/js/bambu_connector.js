/*
 * View model for OctoPrint-BambuConnector
 *
 * Author: jneilliii
 * License: AGPL-3.0-or-later
 */
$(function () {
    function BambuConnectorViewModel(parameters) {
        var self = this;

        self.connectionViewModel = parameters[0];
        self.settingsViewModel = parameters[1];

        self.printOptionsDialog = undefined;
        self.callback = undefined;

        self.useAms = ko.observable(false);
        self.performBedLeveling = ko.observable(false);
        self.performFlowCali = ko.observable(false);
        self.enableTimelapse = ko.observable(false);
        self.plateNumber = ko.observable(1);

        self.onBeforePrintStart = (callback, data) => {
            const connector = self.connectionViewModel.selectedConnector();
            if (connector !== "bambu" || data.origin !== "printer") {
                return;
            }

            if (
                self.settingsViewModel &&
                self.settingsViewModel.settings &&
                self.settingsViewModel.settings.plugins &&
                self.settingsViewModel.settings.plugins.bambu_connector &&
                self.settingsViewModel.settings.plugins.bambu_connector.default_job_params
            ) {
                const params =
                    self.settingsViewModel.settings.plugins.bambu_connector
                        .default_job_params;
                self.useAms(params.use_ams());
                self.performBedLeveling(params.perform_bed_leveling());
                self.performFlowCali(params.perform_flow_cali());
                self.enableTimelapse(params.enable_timelapse());
                self.plateNumber(params.plate_number());
            }

            self.callback = callback;
            self.showPrintOptions();
            return false;
        };

        self.acceptPrintOptions = () => {
            if (!self.callback) return;
            const callback = self.callback;
            self.callback = undefined;

            self.hidePrintOptions();
            callback({
                use_ams: self.useAms(),
                perform_bed_leveling: self.performBedLeveling(),
                perform_flow_cali: self.performFlowCali(),
                enable_timelapse: self.enableTimelapse(),
                plate_number: self.plateNumber()
            });
        };

        self.onRenderParametersForConnector = (connector, parameters) => {
            if (connector !== "bambu") return false;

            return [
                {
                    name: gettext("Host"),
                    value: parameters.host || "-"
                },
                {
                    name: gettext("Serial"),
                    value: parameters.serial || "-"
                },
                {
                    name: gettext("Access Code"),
                    value: parameters.access_code ? "*****" : "-"
                }
            ];
        };

        self.cancelPrintOptions = () => {
            self.hidePrintOptions();
        };

        self.showPrintOptions = () => {
            self.printOptionsDialog.modal("show");
        };

        self.hidePrintOptions = () => {
            self.printOptionsDialog.modal("hide");
        };

        self.onStartup = () => {
            self.printOptionsDialog = $("#bambu_connector_print_options");
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BambuConnectorViewModel,
        dependencies: ["connectionViewModel", "settingsViewModel"],
        elements: ["#settings_plugin_bambu_connector", "#bambu_connector_print_options"]
    });
});
