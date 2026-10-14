"""
Read report files straight from a Windows/Samba share (SMB2/3) without mounting it.

Used by fluent-monitor-web when the folder is given as //server/share/path. Needs the
`smbprotocol` package (installed in the Docker image; not needed for local folders).
Credentials come from the environment: SMB_USERNAME, SMB_PASSWORD and optionally
SMB_DOMAIN (or SMB_USERNAME="DOMAIN\\user" / "user@domain").

Files are opened with full sharing (read/write/delete) and closed after every read, so
Fluent can keep writing to them.
"""

import os


def is_smb_path(path):
    """smb://server/share/dir everywhere; //server/share/dir or \\\\server\\share\\dir except on
    Windows, which opens UNC paths itself."""
    if not path:
        return False
    if path.lower().startswith("smb://"):
        return True
    return os.name != "nt" and (path.startswith("//") or path.startswith("\\\\"))


def normalize(path):
    """'smb://server/share/dir', '\\\\server\\share\\dir' or '//server/share/dir/' -> '//server/share/dir'."""
    if path.lower().startswith("smb://"):
        path = path[4:]
    p = path.replace("\\", "/")
    return "//" + p.lstrip("/").rstrip("/")


class SMBFS:
    """The small file API FileReader / Monitor need, over SMB."""

    def __init__(self, username=None, password=None, domain=None, port=445):
        try:
            import smbclient
        except ImportError as e:      # pragma: no cover - depends on the environment
            raise SystemExit("Reading from //server/share needs the 'smbprotocol' package "
                             "(it is included in the Docker image).") from e
        self._smb = smbclient
        username = username if username is not None else os.environ.get("SMB_USERNAME")
        password = password if password is not None else os.environ.get("SMB_PASSWORD")
        domain = domain if domain is not None else os.environ.get("SMB_DOMAIN")
        if username and domain and "\\" not in username and "@" not in username:
            username = f"{domain}\\{username}"
        port = int(os.environ.get("SMB_PORT", port))
        self._kw = {"username": username, "password": password, "port": port}
        smbclient.ClientConfig(username=username, password=password)

    @staticmethod
    def _unc(path):
        return "\\\\" + normalize(path)[2:].replace("/", "\\")

    def _call(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except OSError:
            raise
        except Exception as e:        # smbprotocol errors (lost VPN, expired session, ...)
            try:
                self._smb.reset_connection_cache()
            except Exception:
                pass
            raise OSError(str(e)) from e

    # ---- the API used by core.FileReader and web.Monitor
    @staticmethod
    def join(folder, name):
        return normalize(folder) + "/" + name

    def listdir(self, folder):
        return self._call(self._smb.listdir, self._unc(folder), **self._kw)

    def isdir(self, folder):
        try:
            return self._call(self._smb.path.isdir, self._unc(folder), **self._kw)
        except OSError:
            return False

    def getsize(self, path):
        return self._call(self._smb.stat, self._unc(path), **self._kw).st_size

    def read_range(self, path, offset, n):
        def read():
            with self._smb.open_file(self._unc(path), mode="rb", share_access="rwd", **self._kw) as fh:
                fh.seek(offset)
                return fh.read(n)
        return self._call(read)
