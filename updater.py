"""Find newer installers on GitHub Releases and hand off a verified in-place update.

The packaged app downloads the release installer, verifies its SHA-256 digest, then starts
it silently with /UPDATE. The installer waits for this app to exit, replaces its files,
refreshes the installed driver without changing the tracking source, writes a result file,
and reopens the app whether or not the update succeeded.

Environment overrides for testing: BEYOND_SLAM_UPDATE_REPO (owner/name),
BEYOND_SLAM_UPDATE_TOKEN (GitHub token for a private repository),
BEYOND_SLAM_UPDATE_API (an HTTPS API base, or http on loopback for local tests) and
BEYOND_SLAM_VERSION_OVERRIDE (pretend the running version is older).
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from typing import NamedTuple
import urllib.error
import urllib.parse
import urllib.request

from release_version import UPDATE_REPOSITORY, VERSION
import sdk_setup

GITHUB_API = "https://api.github.com"
TRUSTED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_INSTALLER = 512 * 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024
DOWNLOAD_DEADLINE = 30 * 60
INSTALLER_ASSET = re.compile(r"^Beyond-SLAM[\w.+-]*\.exe$", re.IGNORECASE)
CHECKSUM_ASSETS = ("SHA256SUMS", "SHA256SUMS.txt")
VERSION_PATTERN = re.compile(r"v?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


class UpdateError(Exception):
    """A problem worth showing to the user as written."""


class UpdateCancelled(UpdateError):
    pass


class Update(NamedTuple):
    version: str
    notes: str
    page_url: str
    asset_name: str
    download_url: str
    api_url: str
    size: int
    sha256: str


def current_version():
    return os.environ.get("BEYOND_SLAM_VERSION_OVERRIDE", "").strip() or VERSION


def parse_version(text):
    """Return a comparable tuple; a pre-release sorts before its final release."""
    match = VERSION_PATTERN.fullmatch(str(text).strip())
    if not match:
        return None
    numbers = tuple(int(part) for part in match[1].split("."))
    return numbers + (0,) * (4 - len(numbers)) + ((0,) if match[2] else (1,))


def is_newer(candidate, current):
    candidate, current = parse_version(candidate), parse_version(current)
    return candidate is not None and current is not None and candidate > current


def repository():
    name = os.environ.get("BEYOND_SLAM_UPDATE_REPO", "").strip() or UPDATE_REPOSITORY
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise UpdateError("The update source is not configured correctly.")
    return name


def api_base():
    base = os.environ.get("BEYOND_SLAM_UPDATE_API", "").strip().rstrip("/") or GITHUB_API
    parts = urllib.parse.urlsplit(base)
    if not (parts.scheme == "https" or parts.scheme == "http" and parts.hostname in LOOPBACK):
        raise UpdateError("The update source must use HTTPS.")
    return base


def trusted(url):
    parts = urllib.parse.urlsplit(url)
    custom = urllib.parse.urlsplit(api_base())
    if parts.scheme == custom.scheme and parts.hostname == custom.hostname and parts.port == custom.port:
        return True
    return parts.scheme == "https" and parts.port in (None, 443) and parts.hostname in TRUSTED_HOSTS


class TrustedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if not trusted(new_url):
            raise UpdateError("The update was redirected to an unexpected server, so it was not downloaded.")
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_url(url, accept, timeout):
    if not trusted(url):
        raise UpdateError("The update points to an unexpected server, so it was not downloaded.")
    opener = urllib.request.build_opener(TrustedRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"Beyond-SLAM/{VERSION}",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    token = os.environ.get("BEYOND_SLAM_UPDATE_TOKEN", "").strip()
    if token:
        # Unredirected headers are never forwarded to the download CDN.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise UpdateError("No published update was found. The update server may be private or unavailable.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub is limiting update checks right now. Try again in an hour.") from exc
        if exc.code in (401,):
            raise UpdateError("The update server rejected the request. Check the update token.") from exc
        raise UpdateError(f"The update server returned an error ({exc.code}). Try again later.") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise UpdateError("GitHub's certificate could not be verified. Check the PC's date and time, then try again.") from exc
        raise UpdateError("Couldn't reach GitHub. Check your internet connection and try again.") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError("Couldn't reach GitHub. Check your internet connection and try again.") from exc


def read_limited(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("The update server sent an unexpectedly large response.")
    return data


def checksum_for(text, name):
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


def release_update(document, current):
    """Return (Update, checksum asset or None) for a newer release, or None.

    Raises UpdateError when a newer release exists but can't be installed safely."""
    if not isinstance(document, dict) or document.get("draft") or document.get("prerelease"):
        return None
    tag = document.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise UpdateError("The latest release has a version number this app doesn't recognize.")
    version = tag.strip().lstrip("vV")
    if not is_newer(version, current):
        return None
    assets = [asset for asset in document.get("assets") or [] if isinstance(asset, dict) and isinstance(asset.get("name"), str)]
    installers = [asset for asset in assets if INSTALLER_ASSET.fullmatch(asset["name"])]
    installers.sort(key=lambda asset: (asset["name"].lower() != "beyond-slam-setup.exe", asset["name"]))
    if not installers:
        raise UpdateError(f"Version {version} doesn't include a Windows installer yet. Try again later.")
    asset = installers[0]
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER:
        raise UpdateError(f"The version {version} installer has an invalid size.")
    digest = asset.get("digest") or ""
    sha256 = digest.split(":", 1)[1].lower() if isinstance(digest, str) and digest.lower().startswith("sha256:") else ""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        sha256 = ""
    notes = document.get("body") if isinstance(document.get("body"), str) else ""
    page = document.get("html_url") if isinstance(document.get("html_url"), str) else ""
    update = Update(version, notes.strip()[:20000], page, asset["name"], str(asset.get("browser_download_url") or ""),
                    str(asset.get("url") or ""), size, sha256)
    checksums = next((item for item in assets if item["name"] in CHECKSUM_ASSETS), None)
    return update, checksums


def check_for_update(current=None, timeout=15):
    """Return an Update when GitHub has a newer, verifiable installer, else None."""
    current = current or current_version()
    with open_url(f"{api_base()}/repos/{repository()}/releases/latest", "application/vnd.github+json", timeout) as response:
        try:
            document = json.loads(read_limited(response, MAX_METADATA).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise UpdateError("GitHub returned an unexpected response. Try again later.") from exc
    found = release_update(document, current)
    if found is None:
        return None
    update, checksums = found
    if not update.sha256 and checksums is not None:
        url = checksums.get("url") if os.environ.get("BEYOND_SLAM_UPDATE_TOKEN") else checksums.get("browser_download_url")
        with open_url(str(url or ""), "application/octet-stream", timeout) as response:
            update = update._replace(sha256=checksum_for(read_limited(response, 65536).decode("utf-8", "replace"), update.asset_name))
    if not update.sha256:
        raise UpdateError(f"Version {update.version} has no checksum, so it can't be verified. Download it from the release page instead.")
    return update


def updates_folder():
    return sdk_setup.data_folder() / "updates"


def download_update(update, progress=None, cancelled=None):
    """Download and verify the installer; returns its path. progress(done, total) runs on this thread."""
    folder = updates_folder()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"Beyond-SLAM-Setup-{re.sub(r'[^0-9A-Za-z.+-]', '_', update.version)}.exe"
    if target.is_file() and target.stat().st_size == update.size and file_sha256(target) == update.sha256:
        return target
    partial = target.with_suffix(".part")
    url = update.api_url if os.environ.get("BEYOND_SLAM_UPDATE_TOKEN") and update.api_url else update.download_url
    started = time.monotonic()
    digest, total = hashlib.sha256(), 0
    try:
        with open_url(url, "application/octet-stream", 30) as response, partial.open("wb") as stream:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) != update.size:
                raise UpdateError("The update server sent a file of the wrong size. Try again later.")
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise UpdateCancelled("Update cancelled.")
                block = response.read(256 * 1024)
                if not block:
                    break
                total += len(block)
                if total > update.size or time.monotonic() - started > DOWNLOAD_DEADLINE:
                    raise UpdateError("The update download was larger or slower than expected. Try again later.")
                digest.update(block)
                stream.write(block)
                if progress is not None:
                    progress(total, update.size)
        if total != update.size:
            raise UpdateError("The update download was incomplete. Check your connection and try again.")
        if digest.hexdigest() != update.sha256:
            raise UpdateError("The downloaded update failed its integrity check, so it was discarded. Try again.")
        with partial.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise UpdateError("The downloaded update is not a Windows installer, so it was discarded.")
        os.replace(partial, target)
        return target
    except OSError as exc:
        raise UpdateError("The update couldn't be saved. Check free disk space and try again.") from exc
    finally:
        partial.unlink(missing_ok=True)


def file_sha256(path):
    try:
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return ""


def can_install():
    """Self-update replaces a packaged install; source checkouts update with git instead."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def installed_folder():
    return Path(sys.executable).resolve().parent


def marker_paths():
    folder = sdk_setup.data_folder()
    return folder / "update-pending.json", folder / "update-result.txt"


def launch_installer(installer, version, app_folder=None, popen=subprocess.Popen):
    """Start the installer detached. The caller must exit promptly so it can replace files."""
    app_folder = Path(app_folder or installed_folder())
    marker, result = marker_paths()
    marker.parent.mkdir(parents=True, exist_ok=True)
    result.unlink(missing_ok=True)
    marker.write_text(json.dumps({"version": version, "from": current_version(), "started": time.time()}), encoding="utf-8")
    command = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS", "/UPDATE",
               f"/DIR={app_folder}", f"/LOG={sdk_setup.data_folder() / 'update-install.log'}",
               f"/RESULT={result}", f"/RELAUNCH={app_folder / 'BeyondSLAMControl.exe'}"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        popen(command, cwd=str(Path(installer).parent), close_fds=True, creationflags=flags)
    except OSError as exc:
        marker.unlink(missing_ok=True)
        raise UpdateError("The update installer couldn't start. Try again, or download it from the release page.") from exc
    logging.info("Started update installer for %s", version)


def consume_update_result(current=None, now=None):
    """After a relaunch, report the finished update once as (succeeded, message), else None."""
    current, now = current or current_version(), time.time() if now is None else now
    marker, result = marker_paths()
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
        target, started = str(document["version"]), float(document.get("started", 0))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        marker.unlink(missing_ok=True)
        return None
    try:
        outcome = result.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        outcome = ""
    if not outcome and now - started < 10 * 60 and is_newer(target, current):
        return None  # The installer may still be running.
    marker.unlink(missing_ok=True)
    result.unlink(missing_ok=True)
    for leftover in updates_folder().glob("Beyond-SLAM-Setup-*"):
        try:
            leftover.unlink()
        except OSError:
            pass
    if outcome.startswith("success") or not outcome and not is_newer(target, current):
        notice = outcome.partition(":")[2].strip()
        message = f"Updated to Beyond-SLAM {target}."
        if notice:
            message += f" The tracking driver still needs attention: {notice}"
        return True, message
    return False, (f"The update to {target} didn't finish, so your current version was kept. "
                   "Close SteamVR, then try again from Settings → Updates.")
