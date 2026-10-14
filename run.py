"""Install and start MenuMind with one command, the same on macOS, Linux and Windows:

    python3 run.py        (Windows: py run.py)

Creates .venv, installs requirements.txt into it, asks for your OpenAI API key unless one is
already set (typed hidden and kept only in this process; nothing is written to disk), then
serves the app at http://127.0.0.1:8000. Press Enter at the prompt to run with chat off.
"""
import getpass
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"MenuMind needs Python 3.11 or newer; this is {sys.version.split()[0]}.")
HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

if not PY.exists():
    print("Creating .venv ...")
    try:
        venv.create(VENV, with_pip=True)
    except subprocess.CalledProcessError:  # Debian/Ubuntu ship venv without ensurepip
        shutil.rmtree(VENV, ignore_errors=True)
        sys.exit("Could not create .venv. On Debian or Ubuntu: sudo apt install python3-venv")
print("Installing requirements ...")
subprocess.run([str(PY), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")],
               check=True)

env = dict(os.environ)
if not (env.get("OPENAI_API_KEY") or env.get("ANTHROPIC_API_KEY")):
    key = getpass.getpass("OpenAI API key (hidden; press Enter to run with chat off): ").strip()
    if key:
        env["OPENAI_API_KEY"] = key
print("MenuMind is starting: open http://127.0.0.1:8000 (Ctrl+C stops it)")
try:
    subprocess.run([str(PY), "-m", "uvicorn", "server:app", "--port", "8000"], cwd=HERE, env=env)
except KeyboardInterrupt:  # Ctrl+C has already stopped the server
    pass
