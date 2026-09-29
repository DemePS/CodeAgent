"""GUARD_SOURCE: the audit-hook sandbox run_python wraps around the project's code."""

GUARD_SOURCE = r"""
import os, re, runpy, sys, tempfile

BLOCKED_EVENTS = {  # starting, replacing or killing processes
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.forkpty", "os.startfile", "_winapi.CreateProcess", "os.kill", "os.killpg",
}
# C functions reachable through ctypes that start or kill processes or change files, which would
# bypass the Python-level checks below (libc / kernel32 / shell32).
BLOCKED_SYMBOLS = re.compile(
    r"^_?(system|popen|exec\w*|fork\w*|vfork|clone\d?|posix_spawn\w*|spawn\w*|kill\w*|"
    r"CreateProcess\w*|WinExec|ShellExecute\w*|TerminateProcess|"
    r"unlink\w*|remove|rmdir|mkdir\w*|CreateDirectory\w*|rename\w*|f?truncate\w*|f?chmod\w*|f?chown\w*|"
    r"f?open\w*|freopen|creat\w*|DeleteFile\w*|RemoveDirectory\w*|MoveFile\w*|ReplaceFile\w*|"
    r"SetFileAttributes\w*)$"
)

# Files may only be written in cache folders and in the temp folder (unless the workspace itself is
# there); the workspace and everything else is read-only.
TEMP_DIR = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
WORKSPACE_DIR = os.path.normcase(os.path.abspath(os.environ["AGENT_GUARD_WORKSPACE"]))
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
FILE_EVENTS = {  # event -> indexes of the path arguments it changes
    "os.remove": (0,), "os.rmdir": (0,), "os.truncate": (0,), "shutil.rmtree": (0,),
    "os.rename": (0, 1), "os.link": (1,), "os.symlink": (1,),
    "os.chmod": (0,), "os.chown": (0,), "os.chflags": (0,), "os.mkdir": (0,), "os.utime": (0,),
}

def writable(path):
    if isinstance(path, int):  # an already-open file descriptor
        return True
    p = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    parts = re.split(r"[\\/]", p)
    if p == os.path.normcase(os.devnull) or any(
        part in CACHE_DIRS or part.startswith("pytest-cache-files-") for part in parts  # pytest's cache staging
    ):
        return True
    if p == WORKSPACE_DIR or p.startswith(WORKSPACE_DIR + os.sep):
        return False
    return p == TEMP_DIR or p.startswith(TEMP_DIR + os.sep)

def deny_file(event, path):
    raise PermissionError(
        f"Blocked by the coding agent: {event} {os.fsdecode(path)!r} -- run_python cannot create, modify, "
        "rename or delete files; use edit_file / write_file / delete_file"
    )

def guard(event, args):
    if event in BLOCKED_EVENTS:
        raise PermissionError(f"Blocked by the coding agent: {event} (starting or stopping processes is not allowed)")
    if event == "ctypes.dlsym" and len(args) > 1 and isinstance(args[1], str) and BLOCKED_SYMBOLS.match(args[1]):
        raise PermissionError(f"Blocked by the coding agent: ctypes access to {args[1]!r} (processes and file changes are not allowed)")
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        writing = (flags & WRITE_FLAGS) if isinstance(flags, int) else any(c in (mode or "") for c in "wax+")
        if writing and not writable(path):
            deny_file("open for writing", path)
    elif event in FILE_EVENTS:
        for i in FILE_EVENTS[event]:
            if i < len(args) and args[i] is not None and not writable(args[i]):
                deny_file(event, args[i])
    elif event == "sqlite3.connect" and args:
        db = os.fsdecode(args[0]) if not isinstance(args[0], str) else args[0]
        in_memory = db in ("", ":memory:") or (db.startswith("file:") and ("mode=memory" in db or "mode=ro" in db))
        if not in_memory and not db.startswith("file:") and not writable(db):
            deny_file("sqlite3.connect", db)

sys.addaudithook(guard)

try:  # the low-level helper behind subprocess is not audited itself -- disable it
    import _posixsubprocess, subprocess
    def _blocked(*a, **k):
        raise PermissionError("Blocked by the coding agent: _posixsubprocess.fork_exec")
    _posixsubprocess.fork_exec = _blocked
    subprocess._fork_exec = _blocked
except ImportError:
    pass

mode, target, *rest = sys.argv[1:]
if mode == "code":
    sys.argv = ["-c", *rest]
    sys.path[0] = ""
    exec(compile(target, "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
elif mode == "module":
    sys.argv = [target, *rest]
    sys.path[0] = ""
    runpy.run_module(target, run_name="__main__", alter_sys=True)
else:
    sys.argv = [target, *rest]
    sys.path[0] = __import__("os").path.dirname(__import__("os").path.abspath(target))
    runpy.run_path(target, run_name="__main__")
"""


