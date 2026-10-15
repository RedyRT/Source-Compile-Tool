<img width="1258" height="145" alt="source_compile_tool_logo_light" src="https://github.com/user-attachments/assets/c5528911-9515-45ca-92ab-5eaf250ac796" />

# Source Compile Tool

A graphical tool based on CompilePalX for batch compiling Source Engine maps. Built with Python and PySide6, it brings map compilation and related processing tasks into one interface.

## Features

- Automatically discovers Source games installed through Steam and supports custom game and tool directories.
- Builds a queue of VMF maps and compiles them for the selected game.
- Provides presets and configurable compilation stages and parameters.
- Supports parameter files in the JSON format (`parameters.json` and `toolsplusplus.json`).
- Displays progress, elapsed time, and tool output; compilation can be cancelled.
- Includes VBSP, VVIS, VRAD, and utility stages for map and game workflows, including packing resources, BSPZIP, copying files, navigation files, cubemaps, launching the game, custom commands, and shutdown.
- Saves interface settings, game directories, and user presets.

> Available stages depend on the selected game and installed tools. You must configure the appropriate Source SDK executables to compile maps.

## Requirements

- Windows.
- Python 3.9 or newer to run from source.
- A Source Engine game and the corresponding compilation tools (such as VBSP, VVIS, and VRAD) for that game's branch.

## Run from source

Open PowerShell or Command Prompt in the project directory and run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

The requirements include PySide6, QtAwesome, pyqtdarktheme, and PyInstaller.

On first launch, the application creates its configuration and parameter files in `config/`. Open **Game & tools directories** to check or add game and tool locations, then select a game, add VMF maps to the queue, and choose a preset.

## Build for Windows

With the dependencies installed, run:

```powershell
.\compile.bat
```

The script builds the application with PyInstaller using `CompilePalX.spec`. The output is created in `dist/Source Compile Tool/`. Run `Source Compile Tool.exe` from that directory. Keep the entire output folder, including `_internal`, when distributing the app.

## Configuration and logs

- `config/presets.json` — compilation presets.
- `config/game_dirs.json` — game and tool directories.
- `config/parameters/` — base and Tools++ parameters
- `config/maps.json` — map queue.
- `logs/source_compile_tool.log` — application log.

Configuration files can be edited manually while the application is closed. Back them up before making changes.

## Parameter compatibility

Parameter files use a format based on CompilePal++. Format compatibility does not guarantee that every parameter, game, or Source SDK version is supported. Use tools and flags appropriate for your game.
