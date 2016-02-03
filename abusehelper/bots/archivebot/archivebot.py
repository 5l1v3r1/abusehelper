import os
import re
import gzip
import json
import time
import errno
import random
import urllib
import contextlib

from datetime import datetime

import idiokit
from idiokit.xmpp.jid import JID
from abusehelper.core import bot, events, taskfarm, utils


def _create_compress_path(path):
    head, tail = os.path.split(path)

    while True:
        new_tail = "{0}.compress-{1:08x}".format(tail, random.getrandbits(32))
        new_path = os.path.join(head, new_tail)

        if not os.path.isfile(new_path):
            return new_path


def _split_compress_path(path):
    r"""
    Return the rotated file path split to the (directory, filename) tuple, where
    the temporary .compress-******** part has been removed from the filename.

    >>> _split_compress_path("path/to/test.json.compress-0123abcd")
    ('path/to', 'test.json')

    Raise ValueError for paths that don not look like rotated files.

    >>> _split_compress_path("path/to/test.json")
    Traceback (most recent call last):
        ...
    ValueError: invalid filename path/to/test.json
    """

    directory, filename = os.path.split(path)

    match = re.match(r"^(.*)\.compress-[0-9a-f]{8}$", filename, re.I)
    if match is None:
        raise ValueError("invalid filename {0}".format(path))

    filename = match.group(1)
    return directory, filename


def _is_compress_path(path):
    r"""
    Return True if path is a valid rotated file path, False otherwise.

    >>> _is_compress_path("path/to/test.json.compress-1234abcd")
    True
    >>> _is_compress_path("path/to/test.json")
    False
    """

    try:
        _split_compress_path(path)
    except ValueError:
        return False
    return True


@contextlib.contextmanager
def _unique_writable_file(directory, prefix, suffix):
    count = 0
    path = os.path.join(directory, "{0}{1}".format(prefix, suffix))

    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except OSError as ose:
            if ose.errno != errno.EEXIST:
                raise
            count += 1
            path = os.path.join(directory, "{0}-{1:08d}{2}".format(prefix, count, suffix))
        else:
            break

    with os.fdopen(fd, "wb") as fileobj:
        yield path, fileobj


def ensure_dir(dir_name):
    r"""
    Ensure that the directory exists (create if necessary) and return
    the absolute directory path.
    """

    dir_name = os.path.abspath(dir_name)
    try:
        os.makedirs(dir_name)
    except OSError, (code, error_str):
        if code != errno.EEXIST:
            raise
    return dir_name


def archive_path(ts, room_name):
    gmtime = time.gmtime(ts)

    return os.path.join(
        room_name.encode("utf-8"),
        time.strftime("%Y", gmtime),
        time.strftime("%m", gmtime),
        time.strftime("%d.json", gmtime)
    )


def open_archive(archive_dir, ts, room_name):
    path = os.path.join(archive_dir, archive_path(ts, room_name))
    dirname = os.path.dirname(path)
    ensure_dir(dirname)
    return open(path, "ab", buffering=1)


def _encode_room_jid(jid):
    """Return sanitized and normalised domain/node path name from
    a bare or a full room JID.
    """
    room_jid = JID(jid)

    if room_jid != room_jid.bare():
        raise ValueError("given room JID does not match with the bare room JID")

    return urllib.quote(unicode(room_jid).encode("utf-8"), safe=" @")


def _rename(path):
    new_path = _create_compress_path(path)
    os.rename(path, new_path)
    return new_path


def compress(path):
    with open(path, "rb") as archive:
        directory, filename = _split_compress_path(path)
        prefix, suffix = os.path.splitext(filename)

        with _unique_writable_file(directory, prefix, suffix + ".gz") as (gz_path, gz_file):
            compressed = gzip.GzipFile(fileobj=gz_file)
            try:
                compressed.writelines(archive)
            finally:
                compressed.close()

    try:
        os.remove(path)
    except OSError:
        pass

    return gz_path


@idiokit.stream
def rotate(event):
    last = None

    while True:
        now = datetime.utcnow().day
        if now != last:
            last = now
            yield idiokit.send(event)

        yield idiokit.sleep(1.0)


class ArchiveBot(bot.ServiceBot):
    archive_dir = bot.Param("directory where archive files are written")

    def __init__(self, *args, **kwargs):
        super(ArchiveBot, self).__init__(*args, **kwargs)

        self.rooms = taskfarm.TaskFarm(self.handle_room, grace_period=0.0)
        self.archive_dir = ensure_dir(self.archive_dir)

    @idiokit.stream
    def session(self, state, src_room):
        src_jid = yield self.xmpp.muc.get_full_room_jid(src_room)
        yield self.rooms.inc(src_jid.bare())

    @idiokit.stream
    def handle_room(self, name):
        msg = "room {0!r}".format(name)

        attrs = events.Event({
            "type": "room",
            "service": self.bot_name,
            "room": unicode(name)
        })

        with self.log.stateful(repr(self.xmpp.jid), "room", repr(name)) as log:
            log.open("Joining " + msg, attrs, status="joining")
            room = yield self.xmpp.muc.join(name, self.bot_name)

            log.open("Joined " + msg, attrs, status="joined")
            try:
                yield idiokit.pipe(room,
                                   events.stanzas_to_events(),
                                   self._archive(room.jid.bare()))
            finally:
                log.close("Left " + msg, attrs, status="left")

    def _archive(self, room_bare_jid):
        compress = utils.WaitQueue()
        room_name = _encode_room_jid(room_bare_jid)

        _dir = os.path.join(self.archive_dir, room_name)

        if _dir != os.path.normpath(_dir):
            raise ValueError("incorrect room name lands outside the archive directory")

        for root, _, filenames in os.walk(_dir):
            for filename in filenames:
                path = os.path.join(root, filename)
                if _is_compress_path(path):
                    compress.queue(0.0, path)

        rotate_event = object()
        collect = idiokit.pipe(
            self._collect(rotate_event, room_name, compress),
            self._compress(compress)
        )
        idiokit.pipe(rotate(rotate_event), collect)
        return collect

    @idiokit.stream
    def _collect(self, rotate_event, room_name, compress):
        archive = None
        try:
            while True:
                event = yield idiokit.next()

                if event is rotate_event:
                    if archive is not None:
                        archive.flush()
                        archive.close()
                        yield compress.queue(0.0, _rename(archive.name))
                        archive = None
                else:
                    if archive is None:
                        archive = open_archive(self.archive_dir, time.time(), room_name)
                        self.log.info("Opened archive {0!r}".format(archive.name))
                    json_dict = dict((key, event.values(key)) for key in event.keys())
                    archive.write(json.dumps(json_dict) + os.linesep)
        finally:
            if archive is not None:
                archive.flush()
                archive.close()
                self.log.info("Closed archive {0!r}".format(archive.name))

    @idiokit.stream
    def _compress(self, queue):
        while True:
            compress_path = yield queue.wait()

            try:
                path = yield idiokit.thread(compress, compress_path)
                self.log.info("Compressed archive {0!r}".format(path))
            except ValueError:
                self.log.error("Invalid path {0!r}".format(compress_path))


if __name__ == "__main__":
    ArchiveBot.from_command_line().execute()
