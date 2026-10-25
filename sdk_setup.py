"""Detect a separately installed Xvisio SDK and launch its official installer."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import ssl
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SDK_VERSION = "3.2.0-20230907"
SDK_DOWNLOAD_URL = "https://download.xvisiotech.com/sdk-download/history_version/V3.2.0-20230907/Select_Platform_Download/For%20windows/XVSDK-3.2.0-msvc2017-x64.exe"
SDK_DOWNLOAD_PAGE = "https://en.xvisiotech.com/downloads"
MAX_DOWNLOAD = 128 * 1024 * 1024


def data_folder():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BSB-SLAM"


def sdk_preferences():
    try:
        document = json.loads((data_folder() / "sdk-location.json").read_text(encoding="utf-8"))
        return document if isinstance(document, dict) else {}
    except (OSError, ValueError):
        return {}


def is_x64_dll(path):
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            offset = struct.unpack_from("<I", header, 60)[0]
            if offset > 1024 * 1024:
                return False
            stream.seek(offset)
            return stream.read(6) == b"PE\0\0\x64\x86"
    except OSError:
        return False


def default_sdk_folder():
    return Path(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files") / "xvsdk"


def sdk_bin():
    override = os.environ.get("XVISIO_SDK_PATH")
    if override:
        candidates = [Path(override)]
    else:
        saved = sdk_preferences().get("folder")
        candidates = ([Path(saved)] if isinstance(saved, str) and saved else []) + [default_sdk_folder()]
    for folder in candidates:
        binary = folder if (folder / "xvsdk.dll").is_file() else folder / "bin"
        if is_x64_dll(binary / "xvsdk.dll"):
            return binary
    return None


def sdk_initial_folder():
    """Browse the detected SDK root, or the nearest existing default-install folder."""
    binary = sdk_bin()
    folder = (binary.parent if binary.name.lower() == "bin" else binary) if binary else default_sdk_folder()
    while not folder.is_dir() and folder.parent != folder:
        folder = folder.parent
    return folder


def select_sdk(folder):
    folder = Path(folder).resolve()
    binary = folder if (folder / "xvsdk.dll").is_file() else folder / "bin"
    if not is_x64_dll(binary / "xvsdk.dll"):
        raise ValueError(f"This folder doesn't contain the Windows x64 Xvisio SDK. Choose its installation folder, usually {default_sdk_folder()}")
    location = data_folder()
    location.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="sdk-location-", suffix=".tmp", dir=location)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"folder": str(binary)}, stream)
        os.replace(temporary, location / "sdk-location.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


class CertificateError(RuntimeError):
    """The vendor's HTTPS certificate failed verification."""


class VendorRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or parsed.hostname != "download.xvisiotech.com" or parsed.port not in (None, 443):
            raise RuntimeError("Xvisio redirected the SDK download to an unexpected server. Use the official download page")
        return super().redirect_request(request, response, code, message, headers, new_url)


def download_sdk():
    folder = data_folder() / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(VendorRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    handle, temporary = tempfile.mkstemp(prefix="xvisio-sdk-", suffix=".part", dir=folder)
    output = Path(temporary).with_suffix(".exe")
    try:
        request = urllib.request.Request(SDK_DOWNLOAD_URL, headers={"User-Agent": "Beyond-SLAM SDK setup"})
        started = time.monotonic()
        digest = hashlib.sha256()
        with os.fdopen(handle, "wb") as stream:
            with opener.open(request, timeout=20) as response:
                size = int(response.headers.get("Content-Length", "0"))
                if size > MAX_DOWNLOAD:
                    raise RuntimeError("The SDK download is larger than expected")
                total = 0
                while block := response.read(256 * 1024):
                    total += len(block)
                    if total > MAX_DOWNLOAD or time.monotonic() - started > 180:
                        raise RuntimeError("SDK download exceeded its size or time limit. Try again later")
                    digest.update(block)
                    stream.write(block)
                if total < 1024 or size and total != size:
                    raise RuntimeError("SDK download was incomplete. Try again")
        with Path(temporary).open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise RuntimeError("The download server did not return a Windows installer")
        os.replace(temporary, output)
        output.with_suffix(".json").write_text(json.dumps({"source": SDK_DOWNLOAD_URL,
            "sha256": digest.hexdigest(), "version": SDK_VERSION}, indent=2), encoding="utf-8")
        return output
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise CertificateError("Xvisio's download certificate could not be verified. Try again later, or install the SDK separately and use Choose SDK folder") from exc
        raise RuntimeError("Could not download the Xvisio SDK. Check your connection and retry, or install it separately and use Choose SDK folder") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def launch_sdk_installer(path):
    """Show the vendor's licence/UAC wizard; do not silently accept its agreement."""
    path = Path(path).resolve()
    if path.suffix.lower() != ".exe" or not path.is_file():
        raise ValueError("Select the Xvisio Windows SDK installer (.exe)")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("The selected file is not a Windows installer")
    class ShellInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    info = ShellInfo()
    info.cbSize, info.fMask, info.lpVerb = ctypes.sizeof(info), 0x140, "open"
    info.lpFile, info.lpDirectory, info.nShow = str(path), str(path.parent), 1
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    try:
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            raise RuntimeError("The SDK installer did not start or was cancelled. Retry when ready")
        if info.hProcess:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            try:
                if kernel.WaitForSingleObject(info.hProcess, 30 * 60 * 1000) != 0:
                    raise RuntimeError("The Xvisio installer is still open. Finish it, then refresh setup")
            finally:
                kernel.CloseHandle(info.hProcess)
        if not sdk_bin():
            raise RuntimeError("Finish the Xvisio SDK installer, then refresh setup. If you chose a custom location, select its installed folder")
    finally:
        if initialized in (0, 1):
            ole.CoUninitialize()


def install_sdk(installer=None):
    """Run the vendor installer, or return "browser" after handing an unverifiable download to the browser."""
    if installer is None:
        try:
            installer = download_sdk()
        except CertificateError:
            # Never run an installer fetched over unverified TLS; the browser shows its own warning and the user decides.
            if not webbrowser.open(SDK_DOWNLOAD_URL):
                raise
            return "browser"
    launch_sdk_installer(installer)
