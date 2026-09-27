# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

from core.parameters import Params
from utilities.open_source.modules import import_run_user_only

from scenarios.windows._library.enterprise_ai.configuration import DEFAULTS


def run():
    Params.setCalculated("scenario_section", "enterprise_ai")
    run_user_only()
    for name, (value, description, options) in DEFAULTS.items():
        Params.setDefault("enterprise_ai", name, value, desc=description, valOptions=options)


def run_user_only():
    libraries = (
        r"Teams\teams_setup", r"Teams\teams_teardown",
        r"enterprise_collab\timers_setup", r"enterprise_collab\timers_teardown",
        r"misc\click_file_explorer", r"misc\click_settings", r"misc\etw_event_tag",
        r"misc\recording_phase_begin", r"misc\recording_phase_end",
        r"productivity\prod_close", r"productivity\prod_kill",
        r"productivity\prod_open", r"productivity\prod_setup",
        r"web\web_check", r"web\web_close_tabs", r"web\web_kill",
        r"web\web_run_12", r"web\web_setup", r"web\web_switchto",
    )
    for library in libraries:
        import_run_user_only("scenarios\\windows\\_library\\" + library)
