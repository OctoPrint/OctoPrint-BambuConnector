/*
 * View model for OctoPrint-BambuConnector
 *
 * Author: jneilliii
 * License: AGPL-3.0-or-later
 */
$(function () {
    function BambuConnectorViewModel(parameters) {
        var self = this;

        self.settings = parameters[0];

        //~~ connection related

        self.lastUpdated = ko.observable(false);

        self.modelOptions = ko.observableArray([]);
        self.currentModel = ko.observable(undefined);

        self.onConnectionDataReceived = (parameters, current, last, preferred) => {
            const models = parameters.bambu.model;

            const currentModel =
                current.connector === "bambu" ? current.parameters.model : undefined;
            const lastModel =
                last.connector === "bambu" ? last.parameters.model : undefined;
            const preferredModel =
                preferred.connector == "bambu" ? preferred.parameters.model : undefined;

            self.modelOptions(models);

            const modelKeys = models.map((item) => item.key);
            if (modelKeys) {
                if (currentModel !== undefined && modelKeys.indexOf(currentModel) >= 0) {
                    self.currentModel(currentModel);
                } else if (lastModel !== undefined && modelKeys.indexOf(lastModel) >= 0) {
                    self.currentModel(lastModel);
                } else if (
                    preferredModel !== undefined &&
                    modelKeys.indexOf(preferredModel) >= 0
                ) {
                    self.currentModel(preferredModel);
                }
            }

            self.lastUpdated(new Date().getTime());
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BambuConnectorViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#connection_options_bambu"]
    });
});
