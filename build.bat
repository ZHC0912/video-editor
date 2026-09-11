@echo off
rem Build the shipping folder into dist\videditor\.
rem
rem Run from the project root. The vendored bin\ffmpeg.exe and bin\ffprobe.exe
rem have to be in place first: they are not in git, and build.spec bundles them
rem by path.

.venv\Scripts\activate && pyinstaller build.spec --noconfirm
