@echo off
rem Opens the "DVD to Jellyfin" window. start lets this console close straight away.
cd /d "C:\github\DVD_Processing"
start "" pythonw -m dvd_processor gui
