import logging
import time

def run(scenario):
    logging.debug('Executing code block: code_1JV2LX8.py')
    """Setup PerfTrack monitoring."""

    scenario._upload("utilities\\proprietary\\ParseUtc\\UtcPerftrack.xml", "C:\\ProgramData\\Microsoft\\Diagnosis\\Sideload", check_modified=False)
    scenario._upload("utilities\\proprietary\\ParseUtc\\DisableAllUploads.json", "C:\\ProgramData\\Microsoft\\Diagnosis\\Sideload", check_modified=False)

    scenario._call(["cmd.exe", '/C reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\DataCollection" /v AllowTelemetry /t REG_DWORD /d 3 /f > null 2>&1'])

    scenario._call(["cmd.exe", '/C reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting" /v DisableWerUpload /t REG_DWORD /d 1 /f > null 2>&1'])

    scenario._call(["cmd.exe", '/C net stop diagtrack >nul 2>&1 & net start diagtrack >nul 2>&1'])
    time.sleep(10)

    scenario._sleep_to_now()
