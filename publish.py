#!/usr/bin/env python3
"""Publishes the blog: Nextcloud -> content/ -> build.py -> FTP(S) hosting.

Runs every 30 minutes as a GitHub Action (.github/workflows/publish.yml),
but works the same on any Linux machine, e.g. from cron:

    python3 publish.py                 # pull, build, upload what changed
    python3 publish.py --dry-run       # pull and build, only *show* the upload
    python3 publish.py --no-pull       # build and upload the local content/
    python3 publish.py --full-upload   # upload everything again (e.g. new hosting)
    python3 publish.py --allow-mass-delete   # see "Safety" below

Settings and passwords come from environment variables (GitHub secrets,
see ENV_SETTINGS below) or from publish.ini next to this script (see
publish.ini.example) -- never from the Nextcloud folder.

How it works:
1. Pull: content/ becomes an exact copy of the Nextcloud folder (WebDAV).
   Only files whose ETag changed are downloaded.
2. Build: runs build.py.
3. Upload: compares public/ with what was uploaded last time (a list of
   checksums in .cache/), uploads new/changed files and deletes removed
   ones. Every file is uploaded under a temporary name and then renamed,
   so visitors never see half-uploaded pages; pictures and styles go up
   before the pages that use them.

Safety: a run that would delete more than a quarter of all files -- in
content/ or on the hosting -- stops instead (an emptied or wrong Nextcloud
folder must not wipe the website). Check what happened, then run once
with --allow-mass-delete if it is really intended. Only one run at a time;
a cron run that finds another one still busy just exits.

Standard library only -- nothing to install besides what build.py needs.
"""
import base64
import configparser
import fcntl
import ftplib
import hashlib
import json
import os
import pathlib
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as etree

ROOT = pathlib.Path(__file__).resolve().parent
CONTENT = ROOT / "content"
PUBLIC = ROOT / "public"
CACHE = ROOT / ".cache"
PULL_STATE = CACHE / "nextcloud-etags.json"
UPLOAD_STATE = CACHE / "uploaded-files.json"
MASS_DELETE_SHARE = 0.25

# Environment variables override publish.ini (on GitHub: repository secrets).
ENV_SETTINGS = {
    "NEXTCLOUD_URL": ("nextcloud", "url"),
    "NEXTCLOUD_USER": ("nextcloud", "user"),
    "NEXTCLOUD_APP_PASSWORD": ("nextcloud", "app_password"),
    "FTP_HOST": ("ftp", "host"),
    "FTP_USER": ("ftp", "user"),
    "FTP_PASSWORD": ("ftp", "password"),
    "FTP_REMOTE_DIR": ("ftp", "remote_dir"),
    "FTP_TLS": ("ftp", "tls"),
    "FTP_PORT": ("ftp", "port"),
}

# Office lock files, OS clutter and hidden files never belong on the website.
IGNORED_PREFIXES = (".", "~$", ".~lock")
IGNORED_NAMES = {"Thumbs.db", "desktop.ini"}

DAV = "{DAV:}"
PROPFIND_BODY = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/></d:prop></d:propfind>"""


def log(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class Stop(Exception):
    """Ends the run with a message, without a traceback."""


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def ignored(relpath):
    return any(part.startswith(IGNORED_PREFIXES) or part in IGNORED_NAMES for part in relpath.split("/"))


def load_config():
    config = configparser.ConfigParser()
    config.read(ROOT / "publish.ini", encoding="utf-8")
    for name, (section, key) in ENV_SETTINGS.items():
        if os.environ.get(name):
            if not config.has_section(section):
                config.add_section(section)
            config[section][key] = os.environ[name]
    return config


def require(config, section, keys):
    missing = [key for key in keys if not config.get(section, key, fallback="").strip()]
    if missing:
        names = {v: k for k, v in ENV_SETTINGS.items()}
        listed = ", ".join(f"{section}.{key} ({names[section, key]})" for key in missing)
        raise Stop(f"missing settings: {listed} -- set them as GitHub secrets or in publish.ini")
    return config[section]


def check_mass_delete(what, deleting, total, allowed):
    if total and len(deleting) > total * MASS_DELETE_SHARE and not allowed:
        sample = ", ".join(sorted(deleting)[:5])
        raise Stop(f"{what}: would delete {len(deleting)} of {total} files ({sample}, ...) -- stopped. "
                   f"If that is really intended, run once with --allow-mass-delete.")


# --------------------------------------------------------------------------
# 1. Pull from Nextcloud (WebDAV)
# --------------------------------------------------------------------------

class Nextcloud:
    def __init__(self, url, user, password):
        self.base = url.rstrip("/") + "/"
        self.base_path = urllib.parse.unquote(urllib.parse.urlsplit(self.base).path)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "User-Agent": "website-publish"}

    def request(self, method, relpath, body=None, extra=None):
        url = self.base + urllib.parse.quote(relpath)
        req = urllib.request.Request(url, data=body, method=method, headers={**self.headers, **(extra or {})})
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise Stop("Nextcloud refused the login -- check the Nextcloud user and app password")
            if error.code == 404 and not relpath:
                raise Stop(f"Nextcloud folder not found -- check the Nextcloud url. {self.where_it_breaks()}")
            if error.code == 405 and not relpath:
                raise Stop("The Nextcloud url is not a WebDAV address -- it must contain "
                           "/remote.php/dav/files/ (not the address from the browser's address bar)")
            raise

    def where_it_breaks(self):
        """For a wrong url: the deepest folder of it that exists and what's in
        there. (GitHub hides the url itself in the log, as it's a secret.)"""
        parts = urllib.parse.urlsplit(self.base)
        segments = self.base_path.strip("/").split("/")
        start = segments.index("files") + 1 if "files" in segments else len(segments) - 1
        for end in range(len(segments) - 1, start, -1):
            path = "/" + "/".join(segments[:end]) + "/"
            req = urllib.request.Request(urllib.parse.urlunsplit((parts.scheme, parts.netloc, urllib.parse.quote(path), "", "")),
                                         data=PROPFIND_BODY, method="PROPFIND",
                                         headers={**self.headers, "Depth": "1", "Content-Type": "application/xml"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    tree = etree.fromstring(resp.read())
            except urllib.error.HTTPError:
                continue
            hrefs = (urllib.parse.unquote(urllib.parse.urlsplit(item.findtext(f"{DAV}href", "")).path).rstrip("/")
                     for item in tree.iter(f"{DAV}response")
                     if item.find(f".//{DAV}resourcetype/{DAV}collection") is not None)
            names = sorted(h.rsplit("/", 1)[-1] for h in hrefs if h != path.rstrip("/"))
            shown = "/".join(segments[start + 1:end]) or "(top level)"
            return (f'Folder "{shown}" exists, but has no "{segments[end]}" '
                    f'(names are case-sensitive). Folders in it: {", ".join(names) or "none"}')
        return (f'Even the top level of user "{segments[start]}" is not reachable -- '
                f'the name after /files/ must be the Nextcloud user id (Files settings -> WebDAV shows it) '
                f'and match the Nextcloud user setting.')

    def listing(self):
        """{relative path: etag} of every file below the folder. Walks one
        level at a time -- Nextcloud doesn't allow "Depth: infinity"."""
        files, pending = {}, [""]
        while pending:
            folder = pending.pop()
            with self.request("PROPFIND", folder, PROPFIND_BODY,
                              {"Depth": "1", "Content-Type": "application/xml"}) as resp:
                tree = etree.fromstring(resp.read())
            for item in tree.iter(f"{DAV}response"):
                href = urllib.parse.unquote(urllib.parse.urlsplit(item.findtext(f"{DAV}href", "")).path)
                if not href.startswith(self.base_path):
                    continue
                relpath = href[len(self.base_path):].strip("/")
                if relpath == folder.strip("/") or ignored(relpath):
                    continue
                if item.find(f".//{DAV}resourcetype/{DAV}collection") is not None:
                    pending.append(relpath + "/")
                else:
                    files[relpath] = item.findtext(f".//{DAV}getetag", "").strip('"') or "?"
        return files

    def download(self, relpath, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".download")
        with self.request("GET", relpath) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
        os.replace(tmp, target)


def pull(config, allow_mass_delete):
    section = require(config, "nextcloud", ("url", "user", "app_password"))
    cloud = Nextcloud(section["url"], section["user"], section["app_password"])
    remote = cloud.listing()
    if not any(p.startswith("posts/") and p.endswith(".md") for p in remote):
        raise Stop("Nextcloud folder has no posts/*.md -- wrong folder in the Nextcloud url? Nothing changed.")

    known = load_json(PULL_STATE)
    local = {p.relative_to(CONTENT).as_posix() for p in CONTENT.rglob("*") if p.is_file()} if CONTENT.exists() else set()
    deleting = {p for p in local if p not in remote and not ignored(p)}
    check_mass_delete("content/", deleting, len(local), allow_mass_delete)

    changed = [p for p, etag in remote.items() if known.get(p) != etag or not (CONTENT / p).is_file()]
    for relpath in sorted(changed):
        cloud.download(relpath, CONTENT / relpath)
        known[relpath] = remote[relpath]
        save_json(PULL_STATE, known)  # a crash later on doesn't redo this file
    for relpath in sorted(deleting):
        (CONTENT / relpath).unlink()
        known.pop(relpath, None)
    for folder in sorted((p for p in CONTENT.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    save_json(PULL_STATE, {p: e for p, e in known.items() if p in remote})
    log(f"pull: {len(changed)} downloaded, {len(deleting)} deleted, {len(remote)} files in Nextcloud")


# --------------------------------------------------------------------------
# 2. Build
# --------------------------------------------------------------------------

def build():
    # build.py reads what editors put into Nextcloud (and decodes their
    # pictures), so it doesn't get the passwords: a bug there can't leak them.
    env = {k: v for k, v in os.environ.items() if k not in ENV_SETTINGS}
    result = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    for line in (result.stdout + result.stderr).strip().splitlines():
        log(f"build: {line}")
    if result.returncode != 0:
        raise Stop("build.py failed -- nothing uploaded, the website stays as it was")


# --------------------------------------------------------------------------
# 3. Upload to the hosting (FTP over TLS)
# --------------------------------------------------------------------------

def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def upload_order(relpath):
    # Pictures, styles and scripts first, pages next, the homepage last --
    # a page never goes live before the files it refers to.
    if relpath == "index.html":
        return (2, relpath)
    return (1 if relpath.endswith(".html") else 0, relpath)


class FTPS(ftplib.FTP_TLS):
    """FTP_TLS that reuses the control connection's TLS session for file
    transfers. Many hosters insist on that ("522 session reuse required"),
    and plain ftplib doesn't do it."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class Hosting:
    def __init__(self, section):
        self.section = section
        self.dirs = set()
        self.ftp = None

    def connect(self):
        s = self.section
        if s.getboolean("tls", True):
            self.ftp = FTPS(context=ssl.create_default_context(), timeout=60)
        else:
            self.ftp = ftplib.FTP(timeout=60)
        self.ftp.connect(s["host"], s.getint("port", 21))
        try:
            self.ftp.login(s["user"], s["password"])
        except ftplib.error_perm:
            raise Stop("the hosting refused the FTP login -- check the FTP user and password")
        if isinstance(self.ftp, ftplib.FTP_TLS):
            self.ftp.prot_p()  # encrypt the files too, not just the password
        try:
            self.ftp.sendcmd("OPTS UTF8 ON")  # file names with umlauts
        except ftplib.Error:
            pass
        self.root = "/" + s.get("remote_dir", "/").strip("/")
        self.dirs = set()
        try:
            self.ftp.cwd(self.root)
        except ftplib.error_perm:
            start = self.ftp.pwd()
            try:
                names = ", ".join(sorted(n.rsplit("/", 1)[-1] for n in self.ftp.nlst(start))) or "nothing"
            except ftplib.Error:
                names = "(can't list)"
            raise Stop(f"FTP folder not found -- check the FTP remote dir. After login the hosting starts "
                       f'in "{start}", which contains: {names}')

    def path(self, relpath):
        return f"{self.root.rstrip('/')}/{relpath}"

    def ensure_dir(self, relpath):
        parts = relpath.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folder = "/".join(parts[:i])
            if folder not in self.dirs:
                try:
                    self.ftp.mkd(self.path(folder))
                except ftplib.error_perm:
                    pass  # exists already
                self.dirs.add(folder)

    def upload(self, relpath):
        self.ensure_dir(relpath)
        target, tmp = self.path(relpath), self.path(relpath) + ".uploading"
        with open(PUBLIC / relpath, "rb") as f:
            self.ftp.storbinary(f"STOR {tmp}", f)
        try:
            self.ftp.rename(tmp, target)
        except ftplib.error_perm:
            # Some servers won't rename onto an existing file.
            self.delete(relpath)
            self.ftp.rename(tmp, target)

    def delete(self, relpath):
        try:
            self.ftp.delete(self.path(relpath))
        except ftplib.error_perm:
            pass  # already gone

    def close(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except (ftplib.Error, OSError):
                self.ftp.close()


def upload(config, full, dry_run, allow_mass_delete):
    current = {p.relative_to(PUBLIC).as_posix(): checksum(p) for p in PUBLIC.rglob("*") if p.is_file()}
    uploaded = {} if full else load_json(UPLOAD_STATE)
    sending = sorted((p for p, h in current.items() if uploaded.get(p) != h), key=upload_order)
    deleting = sorted(p for p in uploaded if p not in current)
    check_mass_delete("hosting", deleting, len(uploaded), allow_mass_delete)

    if not sending and not deleting:
        log("upload: website is up to date")
        return
    if dry_run:
        for relpath in sending:
            log(f"would upload {relpath}")
        for relpath in deleting:
            log(f"would delete {relpath}")
        log(f"dry run: {len(sending)} to upload, {len(deleting)} to delete -- nothing sent")
        return

    hosting = Hosting(require(config, "ftp", ("host", "user", "password")))
    try:
        hosting.connect()
        for i, relpath in enumerate(sending, 1):
            for attempt in (1, 2):
                try:
                    hosting.upload(relpath)
                    break
                except (ftplib.error_temp, ftplib.error_reply, OSError, EOFError):
                    if attempt == 2:
                        raise
                    log(f"upload: connection problem at {relpath}, reconnecting")
                    hosting.close()
                    hosting.connect()
            uploaded[relpath] = current[relpath]
            if i % 25 == 0:
                save_json(UPLOAD_STATE, uploaded)  # an interrupted run resumes here
        for relpath in deleting:
            hosting.delete(relpath)
            uploaded.pop(relpath, None)
    finally:
        save_json(UPLOAD_STATE, uploaded)
        hosting.close()
    log(f"upload: {len(sending)} uploaded, {len(deleting)} deleted")


# --------------------------------------------------------------------------

def main():
    args = set(sys.argv[1:])
    options = {"--dry-run", "--no-pull", "--full-upload", "--allow-mass-delete"}
    if args - options:
        sys.exit(__doc__)
    config = load_config()
    CACHE.mkdir(exist_ok=True)
    with open(CACHE / "publish.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # the previous run is still busy -- that's fine
        try:
            if "--no-pull" not in args:
                pull(config, "--allow-mass-delete" in args)
            build()
            upload(config, "--full-upload" in args, "--dry-run" in args, "--allow-mass-delete" in args)
        except Stop as stop:
            log(f"STOPPED: {stop}")
            sys.exit(1)
        except Exception as error:
            log(f"FAILED: {type(error).__name__}: {error}")
            traceback.print_exc()  # unexpected: show where it happened
            sys.exit(1)


if __name__ == "__main__":
    main()
