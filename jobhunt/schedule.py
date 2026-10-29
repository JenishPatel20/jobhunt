"""macOS per-user 9 a.m. launchd schedule."""
import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.jenishpatel.jobhunt.daily"


def path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(ctx):
    executable = ctx.root / ".venv" / "bin" / "jobhunt"
    if not executable.exists():
        raise RuntimeError(f"Missing {executable}; install the project in .venv first")
    reports = ctx.root / "reports"
    reports.mkdir(exist_ok=True)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "daily"],
        "WorkingDirectory": str(ctx.root),
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": str(reports / "daily.log"),
        "StandardErrorPath": str(reports / "daily-error.log"),
        "EnvironmentVariables": {"JOBHUNT_HOME": str(ctx.root),
                                 "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    target.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Schedule file created but launchctl bootstrap failed: {result.stderr.strip()}")
    print(f"Installed: daily at 9:00 a.m. local time ({target})")


def remove():
    target = path()
    if target.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], capture_output=True)
        target.unlink()
    print("Daily schedule removed")


def status():
    target = path()
    print(f"Schedule file: {'installed' if target.exists() else 'absent'} ({target})")
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    print(f"launchd: {'loaded' if result.returncode == 0 else 'not loaded'}")
