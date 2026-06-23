#!/usr/bin/env python3
"""
Build script to package the Hams.com Local Hardware Relay.
Generates advanced installation scripts that permanently install the daemons
and configure native OS init systems (systemd, launchd, Windows Startup)
to run them automatically in the background on boot.
"""

import os
import json
import urllib.request
import zipfile
import tempfile
import shutil
import logging

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERTIARY_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "hams_private_tertiary"))
DOWNLOADS_DIR = os.path.join(TERTIARY_DIR, "ham_shack", "static", "downloads")
MAC_BIN_DIR = os.path.join(BASE_DIR, "tools", "hamlib_binaries", "macos")
LINUX_BIN_DIR = os.path.join(BASE_DIR, "tools", "hamlib_binaries", "linux")

# Dynamically read the current relay script from the daemons folder
# to ensure the packaged version contains all modern routes (like /setup and /status).
RELAY_SOURCE_PATH = os.path.join(
    BASE_DIR, "daemons", "hams_local_relay", "hams_local_relay.py"
)
with open(RELAY_SOURCE_PATH, "r", encoding="utf-8") as f:
    RELAY_PY = f.read()


def get_user_agent():
    return os.environ.get(
        "SYSTEM_USER_AGENT",
        "Hams.com Bruce Perens K6BP <bruce@perens.com> +1 510-394-5627",
    )


INSTALL_WINDOWS = r"""@echo off
echo =========================================
echo Hams.com Hardware Relay Permanent Installer
echo =========================================
set TARGET_DIR=%LOCALAPPDATA%\HamsRelay
echo [*] Installing to %TARGET_DIR%
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

echo [*] Copying files...
xcopy /Y /E /I ".\*" "%TARGET_DIR%\" >ul

echo [*] Setting up dependencies...
cd /d "%TARGET_DIR%"
pip install flask flask-cors pyserial >ul 2>&1

echo [*] Creating silent background launcher...
echo Set WshShell = CreateObject("WScript.Shell") > "%TARGET_DIR%\launcher.vbs"
echo WshShell.Run chr(34) ^& "%TARGET_DIR%\hamlib\bin\rigctld.exe" ^& chr(34) ^& " -m 1", 0, False >> "%TARGET_DIR%\launcher.vbs"
echo WshShell.Run chr(34) ^& "pythonw.exe" ^& chr(34) ^& " " ^& chr(34) ^& "%TARGET_DIR%\hams_local_relay.py" ^& chr(34), 0, False >> "%TARGET_DIR%\launcher.vbs"

echo [*] Registering Windows Startup Hook...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /Y "%TARGET_DIR%\launcher.vbs" "%STARTUP_DIR%\HamsRelay.vbs" >ul

echo [*] Starting daemons now...
cscript //nologo "%STARTUP_DIR%\HamsRelay.vbs"

echo.
echo [SUCCESS] Hams.com Relay is permanently installed and running in the background.
echo It will automatically start every time you log into Windows.
pause
"""

INSTALL_MACOS = r"""#!/bin/bash
echo "========================================="
echo "Hams.com Hardware Relay Permanent Installer"
echo "========================================="

TARGET_DIR="$HOME/Library/Application Support/HamsRelay"
PLIST_RELAY="$HOME/Library/LaunchAgents/com.hams.relay.plist"
PLIST_RIG="$HOME/Library/LaunchAgents/com.hams.rigctld.plist"

echo "[*] Preparing target directory at $TARGET_DIR..."
mkdir -p "$TARGET_DIR"
cp "$(dirname "$0")/hams_local_relay.py" "$TARGET_DIR/"
if [ -f "$(dirname "$0")/rigctld" ]; then
    cp "$(dirname "$0")/rigctld" "$TARGET_DIR/"
    chmod +x "$TARGET_DIR/rigctld"
fi

echo "[*] Installing dependencies via Homebrew..."
if ! command -v brew &> /dev/null; then
    echo "[!] Homebrew is required but not installed. Please install it from https://brew.sh"
    exit 1
fi
brew install hamlib

echo "[*] Setting up dependencies..."
pip3 install --user flask flask-cors pyserial >/dev/null 2>&1

echo "[*] Generating native launchd services..."
cat <<EOF > "$PLIST_RELAY"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.hams.relay</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>$TARGET_DIR/hams_local_relay.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

cat <<EOF > "$PLIST_RIG"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.hams.rigctld</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(brew --prefix)/bin/rigctld</string>
        <string>-m</string><string>1</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

echo "[*] Loading and starting daemons..."
launchctl unload "$PLIST_RIG" 2>/dev/null || true
launchctl unload "$PLIST_RELAY" 2>/dev/null || true
launchctl load -w "$PLIST_RIG"
launchctl load -w "$PLIST_RELAY"

echo "[SUCCESS] Relay permanently installed and running."
"""

INSTALL_LINUX = r"""#!/bin/bash
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./install_linux.sh)"
  exit 1
fi

echo "========================================="
echo "Hams.com Hardware Relay Permanent Installer"
echo "========================================="

TARGET_DIR="/opt/hams_relay"
echo "[*] Creating $TARGET_DIR..."
mkdir -p $TARGET_DIR
cp "$(dirname "$0")/hams_local_relay.py" "$TARGET_DIR/"

echo "[*] Installing system dependencies..."
apt-get update
apt-get install -y python3-flask python3-flask-cors python3-serial libhamlib-utils

echo "[*] Creating systemd services..."
cat <<EOF > /etc/systemd/system/hams_rigctld.service
[Unit]
Description=Hamlib rigctld Daemon
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/rigctld -m 1
Restart=always
[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/hams_relay.service
[Unit]
Description=Hams.com Web Shack Relay
After=network.target hams_rigctld.service
[Service]
Type=simple
WorkingDirectory=$TARGET_DIR
ExecStart=/usr/bin/python3 $TARGET_DIR/hams_local_relay.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF

echo "[*] Enabling and starting services..."
systemctl daemon-reload
systemctl enable --now hams_rigctld
systemctl enable --now hams_relay

echo "[SUCCESS] Relay permanently installed and running via systemd."
"""


def fetch_latest_hamlib_windows():
    print("[*] Querying GitHub API for latest Hamlib Windows release...")
    api_url = "https://api.github.com/repos/Hamlib/Hamlib/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": get_user_agent()})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
        for asset in data.get("assets", []):
            if asset["name"].startswith("hamlib-w64-") and asset["name"].endswith(
                ".zip"
            ):
                return asset["browser_download_url"], asset["name"]
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _logger.warning("[!] Failed to fetch Hamlib release: %s", e)
    return None, None


def download_file(url, dest):
    print(f"[*] Downloading {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": get_user_agent()})
    with urllib.request.urlopen(req) as response, open(dest, "wb") as out_file:
        shutil.copyfileobj(response, out_file)


def build_packages():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    # 1. macOS Package
    mac_zip_path = os.path.join(DOWNLOADS_DIR, "hams_relay_macos.zip")
    with zipfile.ZipFile(mac_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:  # audit-ignore-path  # fmt: skip
        zf.writestr("hams_local_relay.py", RELAY_PY)

        info = zipfile.ZipInfo("install_macos.command")
        info.external_attr = 0o777 << 16  # Executable
        zf.writestr(info, INSTALL_MACOS)

    print(f"[+] Created {mac_zip_path}")

    # 2. Linux Package
    linux_zip_path = os.path.join(DOWNLOADS_DIR, "hams_relay_linux.zip")
    with zipfile.ZipFile(linux_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:  # audit-ignore-path  # fmt: skip
        zf.writestr("hams_local_relay.py", RELAY_PY)

        info = zipfile.ZipInfo("install_linux.sh")
        info.external_attr = 0o777 << 16  # Executable
        zf.writestr(info, INSTALL_LINUX)

    print(f"[+] Created {linux_zip_path}")

    # 3. Windows Package
    win_url, win_name = fetch_latest_hamlib_windows()
    with tempfile.TemporaryDirectory() as tmpdir:
        win_zip_path = os.path.join(DOWNLOADS_DIR, "hams_relay_windows.zip")
        with zipfile.ZipFile(win_zip_path, "w", zipfile.ZIP_DEFLATED) as target_zip:  # audit-ignore-path  # fmt: skip
            target_zip.writestr("hams_local_relay.py", RELAY_PY)
            target_zip.writestr("install_windows.bat", INSTALL_WINDOWS)

            if win_url:
                local_hamlib_zip = os.path.join(tmpdir, win_name)
                download_file(win_url, local_hamlib_zip)
                print("[*] Extracting Hamlib binaries into Windows package...")
                with zipfile.ZipFile(local_hamlib_zip, "r") as source_zip:  # audit-ignore-path  # fmt: skip
                    for item in source_zip.infolist():
                        extracted_data = source_zip.read(item.filename)
                        parts = item.filename.split("/")
                        if len(parts) > 1:
                            new_path = "hamlib/" + "/".join(parts[1:])
                            if not new_path.endswith("/"):
                                target_zip.writestr(new_path, extracted_data)
    print(f"[+] Created {win_zip_path}")
    print("[*] Build complete!")


if __name__ == "__main__":
    build_packages()
