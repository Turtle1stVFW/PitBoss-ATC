# Installation Guide (Windows 11)

Open-beta testers should run **PitBossATC-Setup.exe** instead of this guide. That installer already includes Python, numpy, and faster-whisper. This page is for a from-source checkout. 

## Quick Start

Prerequisites:

- Python 3.11 (or greater)
  - At a command prompt, type `python --version`.  You should get back your current installed version.  If this does not come back with a number of `Python 3.11` or higher, you will need to follow [Install Python](#install-python).
- SRS
  - As a DCS player, you should already have this installed.  If not, follow [Install DCS-SRS](#install-dcs-srs).
- numpy 2.0 (or greater)
  - At a command prompt, type `python -c "import numpy; print(numpy.__version__)"`.  You should get back a version of `2.0` or higher.  If the command fails (module not found) or the version is lower, follow [Install numpy](#install-numpy).
- whisper (`faster-whisper` 1.0 or greater)
  - At a command prompt, type `python -c "from importlib.metadata import version; print(version('faster-whisper'))"`.  You should get back a version of `1.0` or higher.  If the command fails or the version is lower, follow [Install Whisper](#install-whisper).



## Install Python

PitBoss needs **Python 3.11 or newer** on the Windows machine that runs the Host UI. Do **not** rely on the Microsoft Store `python` stub — install from python.org so `python.exe` is a real interpreter on your PATH.

1. Open [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/) in a browser.
2. Download the latest **Windows installer (64-bit)** for Python **3.11 or higher** (for example *Windows installer (64-bit)* under the current 3.x release).
3. Run the installer.
4. On the first screen, enable **both**:
  - **Add python.exe to PATH**
  - **Install launcher for all users** (optional but recommended)
5. Click **Customize installation** (preferred) or **Install Now**.
6. On **Optional Features**, leave the defaults enabled. Confirm that **tcl/tk and IDLE** is checked (required for the ATC UI).
7. On **Advanced Options**, leave **Install for all users** and **Add Python to environment variables** checked if offered, then click **Install**.
8. When the install finishes, close the installer. Open a **new** Command Prompt or PowerShell window (PATH changes do not apply to windows that were already open).
9. Verify the install:
  ```powershell
   python --version
  ```
   You should see something like `Python 3.11.x` or `Python 3.12.x` (3.11 or greater). If the command is not found, or you only get a Store prompt, reopen a new terminal and check that **Add python.exe to PATH** was enabled; if needed, re-run the installer and choose **Modify** / reinstall with PATH enabled.
10. Optional check for the UI toolkit:
  ```powershell
    python -c "import tkinter; print('tkinter OK')"
  ```
    If that fails, re-run the installer, choose **Modify**, and enable **tcl/tk and IDLE**.



## Install DCS-SRS

PitBoss transmits through SRS. Most DCS players already have **DCS SimpleRadio Standalone (DCS-SRS)** installed; if not, install the official client from Ciribob’s GitHub releases (not from a random mirror).

1. Open the latest release page: [https://github.com/ciribob/DCS-SimpleRadioStandalone/releases/latest](https://github.com/ciribob/DCS-SimpleRadioStandalone/releases/latest)
  (Project home: [https://github.com/ciribob/DCS-SimpleRadioStandalone](https://github.com/ciribob/DCS-SimpleRadioStandalone).)
2. Install the **.NET Desktop Runtime** if the release notes require it (recent SRS builds need **.NET 10**). Use the Windows x64 Desktop Runtime installer linked in the release notes, or from [Microsoft’s .NET download page](https://dotnet.microsoft.com/download).
3. Download **one** of the following from the Assets list on that release:
  - `SRS-AutoUpdater.exe` — simplest; downloads and installs/updates SRS for you, **or**
  - `DCS-SimpleRadioStandalone-*.zip` — manual install (see next steps).
4. If you chose the AutoUpdater: run `SRS-AutoUpdater.exe` and follow the prompts until it finishes.
5. If you chose the zip:
  1. Extract **all** files from the zip into a temporary folder (do not run the installer from inside the zip without extracting).
  2. Run `Installer.exe` from that folder.
  3. Set the **SRS install location** (for example `C:\Program Files\DCS-SimpleRadio-Standalone`).
  4. Point the installer at your **Saved Games** folder (usually `C:\Users\<YourName>\Saved Games`).
  5. Click **Install / Update** and wait until it reports success (the window may look hung until it finishes).
6. Launch **SRS-Client** (or **SR-ClientRadio**) from the Start menu or the install folder.
7. On first run, open **Settings** and set your microphone, speakers/headset, and push-to-talk (PTT) bindings.
8. Confirm it works by connecting to your squadron’s SRS server (host and port from your ops docs — often port **5002**). You should see yourself connected in the client overlay.

Updating later uses the same release page: download the new AutoUpdater or zip and run install/update again; keybindings are normally preserved.

PitBoss ships a patched `DCS-SR-ExternalAudio.exe` in this repo for remote transmit. That is separate from the full SRS client — you still need the normal SRS install above to hear and talk on radio in DCS.

## Install numpy

Voice control needs **numpy 2.0 or newer** (mic buffer and PCM conversion for Whisper). Install it into the same Python you verified under [Install Python](#install-python).

1. Open a **new** Command Prompt or PowerShell window.
2. Confirm Python is on PATH:
  ```powershell
   python --version
  ```
3. Install or upgrade numpy:
  ```powershell
   python -m pip install --upgrade "numpy>=2.0"
  ```
4. Verify:
  ```powershell
   python -c "import numpy; print(numpy.__version__)"
  ```
   You should see `2.0.x` or higher (for example `2.1.0`). If `pip` or `python` is not found, finish [Install Python](#install-python) first and open a new terminal.

You can also install numpy together with Whisper from the repo requirements file — see [Install Whisper](#install-whisper).

## Install Whisper

Local voice control uses `faster-whisper` **1.0 or newer** (not the older `openai-whisper` package). It runs on CPU so the GPU stays free for DCS.

1. Open a **new** Command Prompt or PowerShell window.
2. Confirm Python is on PATH:
  ```powershell
   python --version
  ```
3. Install or upgrade `faster-whisper`:
  ```powershell
   python -m pip install --upgrade "faster-whisper>=1.0"
  ```
4. Verify the package:
  ```powershell
   python -c "from importlib.metadata import version; print(version('faster-whisper'))"
  ```
   You should see `1.0` or higher.
5. Optional first-run model warm-up (downloads `base.en`, about 150 MB, into `%USERPROFILE%\.cache\huggingface\`):
  ```powershell
   python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8'); print('whisper model OK')"
  ```
   No Hugging Face token is required. If you skip this step, the app downloads the model the first time you enable voice control (needs network once).

If import still fails after install, confirm you are using the same `python` that runs the ATC launchers (`python --version` / `where.exe python`) and re-run the `pip` command with that interpreter.