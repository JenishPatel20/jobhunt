"""macOS per-user 9 a.m. launchd schedule."""
import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.jenishpatel.jobhunt.daily"


def path(label=LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def install(ctx, telegram=False):
    executable = ctx.root / ".venv" / "bin" / "jobhunt"
    if not executable.exists():
        raise RuntimeError(f"Missing {executable}; install the project in .venv first")
    reports = ctx.root / "reports"
    reports.mkdir(exist_ok=True)
    label = LABEL + ".telegram" if telegram else LABEL
    target = path(label)
    target.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": [str(executable), "telegram-poll" if telegram else "daily"],
        "WorkingDirectory": str(ctx.root),
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": str(reports / "daily.log"),
        "StandardErrorPath": str(reports / "daily-error.log"),
        "EnvironmentVariables": {"JOBHUNT_HOME": str(ctx.root),
                                 "PATH": str(ctx.root / ".venv" / "bin") + ":" + str(Path.home() / ".local" / "bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    if telegram:
        plist.pop('StartCalendarInterval')
        plist.update(KeepAlive=True, RunAtLoad=True, ThrottleInterval=15,
                     StandardOutPath=str(reports / 'telegram.log'), StandardErrorPath=str(reports / 'telegram-error.log'))
    target.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Schedule file created but launchctl bootstrap failed: {result.stderr.strip()}")
    print(f"Installed: {'Telegram polling while awake' if telegram else 'daily at 9:00 a.m. local time'} ({target})")


def remove(telegram=False):
    target = path(LABEL + ".telegram" if telegram else LABEL)
    if target.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], capture_output=True)
        target.unlink()
    print("Daily schedule removed")


def status(telegram=False):
    label = LABEL + ".telegram" if telegram else LABEL
    target = path(label)
    print(f"Schedule file: {'installed' if target.exists() else 'absent'} ({target})")
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True)
    print(f"launchd: {'loaded' if result.returncode == 0 else 'not loaded'}")
