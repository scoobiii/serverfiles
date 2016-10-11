"""
Server files (``serverfiles``)
==============================

.. index:: server files

Orange server files were created to store large files that do not
come with Orange installation, but may be required for
specific functionality. A typical example is Orange Bioinformatics
add-on, which relies on large data files storing genome information.
These do not come pre-installed, but are rather downloaded from the server
when needed and are stored locally. The module provides functionality for
managing these files.

Server provides files through HTTP with directory indices. Files
can be organised in subfolders. Each file can have
a corresponding info file (with .info extension). The file
must be formatted as a JSON dictionary. The most important keys are
datetime ("%Y-%m-%d %H:%M:%S"), compression (if set, the file is
uncompressed automatically, can be one of .bz2, .gz, .tar.gz, .tar.bz2),
and tags (a list of strings).

Local file management
=====================

.. autoclass:: ServerFiles
    :members:

Remote file management
======================

.. autoclass:: ServerFiles
    :members:

"""

import functools
import urllib.parse
from contextlib import contextmanager
import threading
import os
import tarfile
import gzip
import bz2
import datetime
import tempfile
import json
from html.parser import HTMLParser
import shutil

import requests
import requests.exceptions

from Orange.misc.environ import data_dir


# default socket timeout in seconds
TIMEOUT = 5


#defserver = "http://localhost:9998/"
defserver = "http://193.2.72.57/newsf/"


def _open_file_info(fname):
    with open(fname, 'rt') as f:
        return json.load(f)


def _save_file_info(fname, info):
    with open(fname, 'wt') as f:
        json.dump(info, f)


def _create_path(target):
    try:
        os.makedirs(target)
    except OSError:
        pass


class _FindLinksParser(HTMLParser):

    def __init__(self):
        super().__init__(self)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href":
                    #ignore navidation and hidden files
                    if value.startswith("?") or value.startswith("/") or \
                       value.startswith(".") or value.startswith("__"):
                        continue
                    self.links.append(urllib.parse.unquote(value))


class ServerFiles:

    def __init__(self, username=None, password=None, server=None):
        """
        Creates a ServerFiles instance. Pass your username and password
        to use the repository as an authenticated user. If you want to use
        your access code (as an non-authenticated user), pass it also.
        """
        if not server:
            server = defserver
        self.server = server
        self.username = username
        self.password = password

        self.req = requests.Session()
        a = requests.adapters.HTTPAdapter(max_retries=2)
        self.req.mount('https://', a)
        self.req.mount('http://', a)

        self._searchinfo = None

    def listfiles(self, *args, recursive=True):
        """Return a list of files on the server. Do not list .info files."""
        text = self._open(*args).text
        parser = _FindLinksParser()
        parser.feed(text)
        links = parser.links
        files = [args + (f,) for f in links if not f.endswith("/") and not f.endswith(".info")]
        if recursive:
            for f in links:
                if f.endswith("/"):
                    f = f.strip("/")
                    nargs = args + (f,)
                    files.extend([a for a in self.listfiles(*nargs, recursive=True)])
        return files

    def download(self, *path, target=None, callback=None):
        """
        Download a file and name it with target name. Callback
        is called once for each downloaded percentage.
        """
        _create_path(os.path.dirname(target))

        req = self._open(*path)
        if req.status_code == 404:
            raise FileNotFoundError
        elif req.status_code != 200:
            raise IOError

        fdown = req.raw
        size = int(fdown.getheader('content-length'))

        f = tempfile.TemporaryFile()

        chunksize = 1024*8
        lastchunkreport= 0.0001

        readb = 0
        # in case size == 0 skip the loop
        while size > 0:
            buf = fdown.read(chunksize)
            readb += len(buf)
            while float(readb) / size > lastchunkreport+0.01:
                lastchunkreport += 0.01
                if callback:
                    callback()
            if not buf:
                break
            f.write(buf)

        fdown.close()
        f.seek(0)

        with open(target, "wb") as fo:
            shutil.copyfileobj(f, fo)

        if callback:
            callback()

    def allinfo(self, *path, recursive=True):
        files = self.listfiles(*path, recursive=True)
        infos = {}
        for a in files:
            npath = a
            infos[npath] = self.info(*npath)
        return infos

    def search(self, sstrings, **kwargs):
        """
        Search for files on the repository where all substrings in a list
        are contained in at least one choosen field (tag, title, name). Return
        a list of tuples: first tuple element is the file's domain, second its
        name. As for now the search is performed locally, therefore
        information on files in repository is transfered on first call of
        this function.
        """
        if not self._searchinfo:
            self._searchinfo = self.allinfo()
        return _search(self._searchinfo, sstrings, **kwargs)

    def info(self, *path):
        """Return a dictionary containing repository file info."""
        path = list(path)
        path[-1] += ".info"
        t = self._open(*path)
        if t.status_code == 200:
            return json.loads(t.text)
        else:
            return {}

    def _server_request(self, root, *path):
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)
        return self.req.get(root+"/".join(path), auth=auth, verify=False, timeout=TIMEOUT, stream=True)

    def _open(self, *args):
        return self._server_request(self.server, *args)


def _keyed_lock(lock_constructor=threading.Lock):
    lock = threading.Lock()
    locks = {}
    def get_lock(key):
        with lock:
            if key not in locks:
                locks[key] = lock_constructor()
            return locks[key]
    return get_lock


#using RLock instead of Ales's Orange 2 solution
_get_lock = _keyed_lock(threading.RLock)


def _split_path(head):
    out = []
    while True:
        head, tail = os.path.split(head)
        out.insert(0, tail)
        if not head:
            break
    return out


class LocalFiles:

    def __init__(self, path=None, serverfiles=None):
        self.serverfiles_dir = path
        if self.serverfiles_dir is None:
            self.serverfiles_dir = os.path.join(data_dir(), "serverfiles")
        _create_path(self.serverfiles_dir)
        self.serverfiles = serverfiles
        if self.serverfiles is None:
            self.serverfiles = ServerFiles()

    @contextmanager
    def _lock_file(self, *args):
        path = self.localpath(*args)
        path = os.path.normpath(os.path.realpath(path))
        lock = _get_lock(path)
        lock.acquire(True)
        try:
            yield
        finally:
            lock.release()

    def _locked(f):
        @functools.wraps(f)
        def func(self, *path, **kwargs):
            with self._lock_file(*path):
                return f(self, *path, **kwargs)
        func.unwrapped = f
        return func

    def localpath(self, *args):
        """ Return the local location for a file. """
        return os.path.join(os.path.expanduser(self.serverfiles_dir), *args)

    @_locked
    def download(self, *path, callback=None, extract=True):
        """Downloads file from the repository to local orange installation.
        To download files as an authenticated user you should also pass an
        instance of ServerFiles class. Callback can be a function without
        arguments. It will be called once for each downloaded percent of
        file: 100 times for the whole file."""

        info = self.serverfiles.info(*path)

        extract = extract and "compression" in info
        target = self.localpath(*path)
        self.serverfiles.download(*path,
                                  target=target + ".tmp" if extract else target,
                                  callback=callback)

        _save_file_info(target + '.info', info)

        if extract:
            if info.get("compression") in ["tar.gz", "tar.bz2"]:
                f = tarfile.open(target + ".tmp")
                try:
                    os.mkdir(target)
                except OSError:
                    pass
                f.extractall(target)
            elif info.get("compression") == "gz":
                f = gzip.open(target + ".tmp")
                shutil.copyfileobj(f, open(target, "wb"))
            elif info.get("compression") == "bz2":
                f = bz2.BZ2File(target + ".tmp", "r")
                shutil.copyfileobj(f, open(target, "wb"))
            f.close()
            os.remove(target + ".tmp")

    @_locked
    def localpath_download(self, *path, **kwargs):
        """
        Return local path for the given domain and file. If file does not exist,
        download it. Additional arguments are passed to the :obj:`download` function.
        """
        pathname = self.localpath(*path)
        if not os.path.exists(pathname):
            self.download.unwrapped(self, *path, **kwargs)
        return pathname

    def listfiles(self, *path):
        """List files (or folders) in local repository that have
        corresponding .info files.  Do not list .info files."""
        dir = self.localpath(*path)
        files = []
        for root, dirs, fnms in os.walk(dir):
            for f in fnms:
                if f[-5:] == '.info' and os.path.exists(os.path.join(root, f[:-5])):
                    try:
                        _open_file_info(os.path.join(root, f))
                        files.append(
                            path + tuple(_split_path(
                                os.path.relpath(os.path.join(root, f[:-5]), start=dir)
                            )))
                    except ValueError:
                        pass
        return files

    def info(self, *path):
        """Returns info of a file in a local repository."""
        target = self.localpath(*path)
        return _open_file_info(target + '.info')

    def allinfo(self, *path):
        """Goes through all files in a domain on a local repository and returns a
        dictionary, where keys are names of the files and values are their
        information."""
        files = self.listfiles(*path)
        dic = {}
        for filename in files:
            dic[filename] = self.info(*filename)
        return dic

    def needs_update(self, *path):
        """True if a file does not exist in the local repository
        if there is a newer version on the server or if either
        version can not be determined."""
        dt_fmt = "%Y-%m-%d %H:%M:%S"
        try:
            linfo = self.info(*path)
            dt_local = datetime.datetime.strptime(
                            linfo["datetime"][:19], dt_fmt)
            dt_server = datetime.datetime.strptime(
                self.serverfiles.info(*path)["datetime"][:19], dt_fmt)
            return dt_server > dt_local
        except FileNotFoundError:
            return True
        except KeyError:
            return True

    def update(self, *path, **kwargs):
        """Downloads the corresponding file from the server and places it in
        the local repository if the server copy is updated.
        """
        if self.needs_update(*path):
            self.download(*path, **kwargs)

    def search(self, sstrings, **kwargs):
        """Search for files in the local repository where all substrings in a list
        are contained in at least one chosen field (tag, title, name). Return a
        list of tuples: first tuple element is the domain of the file, second
        its name."""
        si = self.allinfo()
        return _search(si, sstrings, **kwargs)

    def update_all(self, *path):
        for fu in self.listfiles(*path):
            self.update(*fu)

    @_locked
    def remove(self, *path):
        """"Remove a file of a path from local repository."""
        path = self.localpath(*path)
        if os.path.exists(path + ".info"):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.isfile(path):
                    os.remove(path)
                os.remove(path + ".info")
            except OSError as ex:
                print("Failed to delete", path, "due to:", ex)
        else:
            raise FileNotFoundError


def _search(si, sstrings, case_sensitive=False, in_tag=True, in_title=True, in_name=True):
    found = []

    for path, info in si.items():
        target = ""
        if in_tag: target += " ".join(info.get('tags', []))
        if in_title: target += info.get('title', "")
        if in_name: target += " ".join(path)
        if not case_sensitive: target = target.lower()

        match = True
        for s in sstrings:
            if not case_sensitive:
                s = s.lower()
            if s not in target:
                match = False
                break

        if match:
            found.append(path)

    return found


def sizeformat(size):
    """
    >>> sizeformat(256)
    '256 bytes'
    >>> sizeformat(1024)
    '1.0 KB'
    >>> sizeformat(1.5 * 2 ** 20)
    '1.5 MB'

    """
    for unit in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            if unit == "bytes":
                return "%1.0f %s" % (size, unit)
            else:
                return "%3.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f PB" % size


if __name__ == '__main__':
    pass
    sf = ServerFiles()
    lf = LocalFiles()
    print(sf.listfiles())
    #sf.download("wtest file.txt", target="downloaded")
    print("info", sf.info("ogrodje.py"))
    #download("wtest file.txt")

    lf1 = lf.listfiles()
    lf1 = lf.listfiles("Affy")
    print("list", lf1)
    for f in lf1:
        print(lf.info(*f))
    lf.download("GO", "taxonomy.pickle")
    print(sf.allinfo())
    print(sf.search("draw"))
    print(sf.search("test"))
    print(sf.search("blabla"))

    lf.remove("wtest file.txt")