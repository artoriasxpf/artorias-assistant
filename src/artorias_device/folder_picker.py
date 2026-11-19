"""Native macOS folder dialog, exposed to the web UI as a device capability.

The web UI only needs an absolute path so the server can register/authorize the
workspace root. Browsers cannot return a real filesystem path from a directory
picker (`showDirectoryPicker` yields an opaque handle), so this small native
bridge uses `osascript`/NSOpenPanel on macOS.
"""
import subprocess
from threading import Lock

from artorias_shared.workspace import ConnectorUnavailable

_dialog_lock = Lock()
_SCRIPT = """
use framework "AppKit"
use scripting additions

on run
    set hostApp to current application's NSApplication's sharedApplication()
    hostApp's setActivationPolicy:(current application's NSApplicationActivationPolicyAccessory)
    hostApp's activateIgnoringOtherApps:true
    set panel to current application's NSOpenPanel's openPanel()
    panel's setTitle:"选择本地工作空间"
    panel's setPrompt:"选择文件夹"
    panel's setCanChooseFiles:false
    panel's setCanChooseDirectories:true
    panel's setCanCreateDirectories:true
    panel's setAllowsMultipleSelection:false
    if (panel's runModal()) is (current application's NSModalResponseOK) then
        return (panel's URL()'s |path|()) as text
    end if
    return ""
end run
"""


def pick_folder():
    if not _dialog_lock.acquire(blocking=False):
        raise ValueError("文件夹选择窗口已打开")
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _SCRIPT],
            capture_output=True, text=True, check=True, timeout=300,
        )
        return result.stdout.removesuffix("\n") or None
    except (OSError, subprocess.SubprocessError) as error:
        raise ConnectorUnavailable("无法打开 macOS 文件夹选择器，请重试") from error
    finally:
        _dialog_lock.release()
