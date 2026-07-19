# SnapToGMod

SnapToGMod listens to the microphone, detects a finger snap or clap, and opens Garry's Mod through Steam with a connection to the selected server. A global `Ctrl+Alt+G` hotkey is available as a fallback.

## Requirements

- Windows 10 or Windows 11
- Python 3.9 or newer; Python 3.12 is recommended
- Steam and Garry's Mod
- A working microphone

## Install from source

Open PowerShell:

```powershell
git clone https://github.com/SevereClaw/SnapToGMod.git
cd SnapToGMod

py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python SnapSt.py
```

For Command Prompt, activate the environment with:

```bat
.venv\Scripts\activate.bat
```

The application runs in the Windows notification area. Right-click the tray icon to select or add a server, adjust sensitivity, test the microphone, configure the hotkey, and enable autostart.

## Build an executable

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pyinstaller --clean --onefile --noconsole --name SnapToGMod SnapSt.py
```

The result is written to:

```text
dist\SnapToGMod.exe
```

## Configuration and logs

Runtime data is stored outside the repository:

```text
%APPDATA%\SnapToGMod\config.json
%APPDATA%\SnapToGMod\stats.json
%APPDATA%\SnapToGMod\snap_to_gmod.log
```

Delete `config.json` to restore default settings.

## Releases

The repository includes a GitHub Actions workflow that validates the script and builds `SnapToGMod.exe` on Windows. Every tag matching `v*` also creates or updates a GitHub Release and attaches the executable.

Before publishing a new release:

1. Update `APP_VERSION` in `SnapSt.py`.
2. Commit and push the change.
3. Create a semantic-version tag.

```powershell
git tag v1.0.1
git push origin v1.0.1
```

Use tags such as `v1.0.0`, `v1.0.1`, and `v1.1.0`. Do not use a tag named `Release`; the application's update checker expects numeric version tags.

## Troubleshooting

If the microphone is not detected, check Windows microphone permissions and the default input device. If the hotkey does not work while Steam or Garry's Mod is running as administrator, run SnapToGMod as administrator as well.

Detailed errors are written to `%APPDATA%\SnapToGMod\snap_to_gmod.log`.
