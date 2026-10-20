## Developer setup

(For best experience, run the setup and testing flow using bash.)

Need Python version >=3.12; 3.12 is best for developing.

**Linux only — install tkinter at the OS level:**
`uv`-managed Python and many distro Pythons don't bundle tkinter.
DLC needs it for the first-run Digital.jar file-picker dialog and
for 3 file-picker tests in the suite. macOS and Windows ship tkinter
with python.org Python — skip this step there.

```bash
# Debian / Ubuntu
sudo apt install python3-tk
# Fedora / RHEL
sudo dnf install python3-tkinter
# Arch
sudo pacman -S tk
```

**General:**
```bash
# Install uv once (skip if already installed)
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Clone and run tests
git clone <repo-url>
cd digital-lab-coach
uv run pytest
```
After all tests are green you are all set — run the web app with:

```bash
uv sync
uv run python -m dlc.web.server
```

**Side notes**

 1. The shell installer only updates the shell it's run from. If you
    install `uv` via Git Bash but want to use it from PowerShell, run
    the PowerShell installer too.

 2. After install, **close and reopen** your terminal (restart VS Code
    if it still can't find `uv`.)

 3. PowerShell doesn't always parse multi-line `python -c "..."` blocks
    cleanly. For the manual snippets in `docs/dev/test_notes.md`, use Git
    Bash, or save the script to a `.py` file and run `uv run python script.py`.

## Troubleshooting (Windows): `uv run` blocked by Smart App Control

**Symptom** — `uv run python ...` fails *before* the app starts:

```
error: Failed to spawn: `python`
  Caused by: ... (os error 4551)
# or, after forcing a system Python:
Querying Python at `...\WindowsApps\python3.exe` failed (exit code 0x800711c7)
```

`os error 4551`:
an application control policy has blocked this file. Windows 11's **Smart App
Control** can switch itself from Evaluation to On (e.g. after an update),
and then it blocks unsigned executables — including the Python `uv` downloads
(python-build-standalone) and the Microsoft Store `python3.exe` alias stub. A
`.venv` built on a now-blocked interpreter stops launching too. This is an
environment/OS block.

**Fix — install a *signed* Python and rebuild the venv:**

1. **Disable the Store alias stubs** so they stop shadowing the real Python:
   Settings → Apps → Advanced app settings → App execution aliases → turn
   **off** `python.exe`, `python3.exe` and `pythonw.exe`.
2. **Install a signed Python 3.12** from <https://www.python.org> (PSF-signed;
   tick "Add python.exe to PATH"). Verify it isn't blocked: `python --version`.
   If Smart App Control still blocks it, install **Python 3.12 from the
   Microsoft Store** instead — Store apps are always trusted by Smart App Control.
3. **Delete the dead venv and rebuild** against the signed Python (Git Bash):

```bash
rm -rf .venv
uv venv --python "C:/Users/<you>/AppData/Local/Programs/Python/Python312/python.exe"
uv sync
uv run python -m dlc.web.server
```

Don't turn Smart App Control *off* to fix this - it is one-way (you can't
re-enable it without reinstalling Windows). Use a signed Python instead.


## Developer Optional setup: Digital.jar for per-row test verification

DLC's structural analysis works on any `.dig` file with no extra setup.

**For per-row pass/fail diagnostics and failing test analysis**, the tool
runs Digital's CLI as a subprocess, so it needs to know where your `Digital.jar` is.

### Setting it up
Download Digital from
<https://github.com/hneemann/Digital>, extract anywhere, and let the first-run dialog find your jar.

If you'd rather configure it manually:

```bash
# Option A
uv run python -c "from dlc.testing.config import set_digital_jar_path; set_digital_jar_path(r'PATH_TO_YOUR_Digital.jar')"

# Option B
# macOS / Linux
export DIGITAL_JAR=/path_to_Digital/Digital.jar
# Windows PowerShell
$env:DIGITAL_JAR = "C:\path_to_Digital\Digital.jar"
```