# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the Star Citizen telemetry .proto schema from the game.

Waits for StarCitizen.exe to start, reads its file version and the
embedded gRPC telemetry protobuf descriptors from the executable, and
writes them as a single buildable ``sc_telemetry_{version}.proto`` file
next to this script.
"""

import ctypes
import mmap
import struct
import time
from ctypes import wintypes
from pathlib import Path

from google.protobuf import descriptor_pb2

EXE_NAME = "StarCitizen.exe"
CHECK_INTERVAL = 2  # Seconds between checks for the game.

# A definition is printed on one line if it has no nested definitions
# and fits within this many characters.
LINE_LIMIT = 120

# An embedded .proto schema starts with its file name: byte 0x0A, a
# one-byte name length, then the name. Telemetry schemas use this prefix.
_PREFIX = b"sc/game/telemetry/"

# FileDescriptorProto field number -> allowed wire types (0 = varint,
# 2 = length-prefixed). The first field not listed ends the descriptor.
_WIRE_TYPES = {
    1: {2}, 2: {2}, 3: {2}, 4: {2}, 5: {2}, 6: {2}, 7: {2}, 8: {2},
    9: {2}, 10: {0, 2}, 11: {0, 2}, 12: {2}, 14: {0},
}

FDP = descriptor_pb2.FieldDescriptorProto


class _ProcessEntry(ctypes.Structure):
    """Mirror of the Win32 PROCESSENTRY32W structure."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


# Exact signatures so 64-bit handles are passed correctly.
_k32 = ctypes.WinDLL("kernel32")
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = (
    wintypes.HANDLE, ctypes.POINTER(_ProcessEntry),
)
_k32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def find_exe_path() -> str | None:
    """Return the full path of the running StarCitizen.exe, or None."""
    # Snapshot all processes (0x2 = TH32CS_SNAPPROCESS) and find the game.
    snapshot = _k32.CreateToolhelp32Snapshot(0x2, 0)
    entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
    pid = None
    try:
        found = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found and pid is None:
            if entry.szExeFile.casefold() == EXE_NAME.casefold():
                pid = entry.th32ProcessID
            found = _k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    if pid is None:
        return None

    # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION: enough to read the path.
    handle = _k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        ok = _k32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size),
        )
        return buffer.value if ok else None
    finally:
        _k32.CloseHandle(handle)


def _file_version(data: mmap.mmap) -> str | None:
    """Return the exe's "File version" (Properties > Details) as a.b.c.d.

    Read from the VS_FIXEDFILEINFO structure in the version resource:
    the signature 0xFEEF04BD follows the "VS_VERSION_INFO" key, and the
    file version is the two DWORDs 8 bytes after it.
    """
    key = data.rfind("VS_VERSION_INFO".encode("utf-16-le"))
    sig = data.find(b"\xbd\x04\xef\xfe", key) if key != -1 else -1
    if sig == -1:
        return None
    ms, ls = struct.unpack_from("<II", data, sig + 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _varint(data: mmap.mmap, i: int) -> tuple[int, int]:
    """Decode a protobuf varint; return its value and the next offset."""
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def _descriptor_end(data: mmap.mmap, i: int) -> int:
    """Return the offset just past the descriptor that starts at ``i``."""
    size = len(data)
    try:
        while i < size:
            tag, j = _varint(data, i)
            if tag & 7 not in _WIRE_TYPES.get(tag >> 3, ()):
                break
            # Varint fields are skipped by reading them; length-prefixed
            # fields by jumping over their payload.
            value, j = _varint(data, j)
            if tag & 7 == 2:
                j += value
            if j > size:
                break
            i = j
    except IndexError:
        pass  # Ran off the end of the file: the descriptor ends here.
    return i


def read_exe(path: str) -> tuple[str | None, dict]:
    """Return the exe's file version and its telemetry descriptors.

    The descriptors are keyed by .proto file name, in the order they
    appear. The exe is memory-mapped read-only, so only the pages
    searched are loaded; the map and file are closed before returning.
    """
    files = {}
    with (
        open(path, "rb") as file,
        mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        version = _file_version(data)
        hit = data.find(_PREFIX)
        while hit != -1:
            start = hit - 2
            name = data[hit:hit + data[hit - 1]].decode("ascii", "replace")
            if (start >= 0 and data[start] == 0x0A
                    and name.endswith(".proto") and name not in files):
                fdp = descriptor_pb2.FileDescriptorProto()
                try:
                    end = _descriptor_end(data, start)
                    fdp.ParseFromString(data[start:end])
                except Exception:
                    pass  # Just a matching string, not a descriptor.
                # Keep only real schemas, not bare file-name matches.
                if fdp.message_type or fdp.enum_type or fdp.service:
                    files[name] = fdp
            hit = data.find(_PREFIX, hit + 1)
    return version, files


def _short(type_name: str, package: str, scope: list[str]) -> str:
    """Return the shortest name that refers to ``type_name`` in scope."""
    name = type_name.lstrip(".").removeprefix(package + ".")
    for depth in range(len(scope), 0, -1):
        prefix = ".".join(scope[:depth]) + "."
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _block(depth: int, head: str, items: list) -> list[str]:
    """Render ``head { items }`` as indented lines.

    Items are statement strings or already-rendered nested blocks. The
    block is a single line if it has no nested blocks and fits within
    LINE_LIMIT, otherwise one line per statement.
    """
    pad = "  " * depth
    if all(isinstance(item, str) for item in items):
        line = f"{pad}{head} {{ {' '.join(items)} }}" if items else (
            f"{pad}{head} {{}}"
        )
        if len(line) <= LINE_LIMIT:
            return [line]
    lines = [f"{pad}{head} {{"]
    for item in items:
        lines.extend([f"{pad}  {item}"] if isinstance(item, str) else item)
    return lines + [f"{pad}}}"]


def _reserved(ranges, names, end_offset: int) -> list[str]:
    """Return ``reserved`` statements; ``end_offset`` 1 = exclusive ends."""
    parts = [
        str(r.start) if r.start == r.end - end_offset
        else f"{r.start} to {r.end - end_offset}"
        for r in ranges
    ]
    lines = [f"reserved {', '.join(parts)};"] if parts else []
    if names:
        quoted = ", ".join(f'"{n}"' for n in names)
        lines.append(f"reserved {quoted};")
    return lines


def _field(field, package: str, scope: list[str], maps: dict) -> str:
    """Return a field definition, e.g. ``repeated string names = 3;``."""
    def type_of(f) -> str:
        if f.type in (FDP.TYPE_MESSAGE, FDP.TYPE_ENUM):
            return _short(f.type_name, package, scope)
        return FDP.Type.Name(f.type)[len("TYPE_"):].lower()

    repeated = field.label == FDP.LABEL_REPEATED
    entry = maps.get(field.type_name.rsplit(".", 1)[-1]) if repeated else None
    if entry is not None:
        kind = f"map<{type_of(entry.field[0])}, {type_of(entry.field[1])}>"
    else:
        label = ("repeated " if repeated
                 else "optional " if field.proto3_optional else "")
        kind = label + type_of(field)
    return f"{kind} {field.name} = {field.number};"


def _enum(enum, depth: int) -> list[str]:
    """Render an enum definition as indented lines."""
    items = [f"{v.name} = {v.number};" for v in enum.value]
    items += _reserved(enum.reserved_range, enum.reserved_name, 0)
    return _block(depth, f"enum {enum.name}", items)


def _message(message, depth: int, package: str, scope: list[str]) -> list:
    """Render a message definition, including nested types, as lines."""
    inner = scope + [message.name]
    # Map fields are stored as hidden nested "entry" messages.
    maps = {n.name: n for n in message.nested_type if n.options.map_entry}
    items: list = [_enum(e, depth + 1) for e in message.enum_type]
    items += [
        _message(n, depth + 1, package, inner)
        for n in message.nested_type if n.name not in maps
    ]

    # Oneofs created only to support proto3 "optional" are not printed.
    real = {
        f.oneof_index for f in message.field
        if f.HasField("oneof_index") and not f.proto3_optional
    }
    printed = set()
    for f in message.field:
        index = f.oneof_index if f.HasField("oneof_index") else None
        if index not in real:
            items.append(_field(f, package, inner, maps))
        elif index not in printed:
            printed.add(index)
            members = [
                _field(m, package, inner, maps) for m in message.field
                if m.HasField("oneof_index") and m.oneof_index == index
            ]
            items.append(_block(
                depth + 1, f"oneof {message.oneof_decl[index].name}", members,
            ))
    items += _reserved(message.reserved_range, message.reserved_name, 1)
    return _block(depth, f"message {message.name}", items)


def _service(service, package: str) -> list[str]:
    """Render a gRPC service definition as lines."""
    items = []
    for m in service.method:
        request = ("stream " if m.client_streaming else "") + _short(
            m.input_type, package, [])
        response = ("stream " if m.server_streaming else "") + _short(
            m.output_type, package, [])
        items.append(f"rpc {m.name}({request}) returns ({response});")
    return _block(0, f"service {service.name}", items)


def render_proto(files: dict, comment_lines: list[str]) -> str:
    """Merge the descriptors and rebuild them as one proto3 source text.

    All files share one package, so imports between them are dropped.
    Options and original comments are not stored in a form worth
    printing, so only ``comment_lines`` is added, under the header.
    """
    first = next(iter(files.values()))
    package = first.package
    lines = [f'syntax = "{first.syntax}";', f"package {package};"]
    imports = {d for f in files.values() for d in f.dependency} - set(files)
    lines += [f'import "{name}";' for name in sorted(imports)]
    lines += ["", *(f"// {line}".rstrip() for line in comment_lines), ""]

    # All enums first, then messages, then services, file by file.
    blocks = [_enum(e, 0) for f in files.values() for e in f.enum_type]
    blocks += [
        _message(m, 0, package, [])
        for f in files.values() for m in f.message_type
    ]
    blocks += [_service(s, package) for f in files.values() for s in f.service]

    # One-line definitions sit together; a blank line separates anything
    # that spans several lines.
    previous = None
    for block in blocks:
        if previous and (len(previous) > 1 or len(block) > 1):
            lines.append("")
        lines.extend(block)
        previous = block
    return "\n".join(lines) + "\n"


def build(exe_path: str) -> tuple[str | None, str | None]:
    """Return the exe's file version and its rebuilt .proto text.

    The text is None if the exe has no proto3 telemetry schema.
    """
    version, files = read_exe(exe_path)
    if not files or any(f.syntax != "proto3" for f in files.values()):
        return version, None
    services = ", ".join(s.name for f in files.values() for s in f.service)
    channel = Path(exe_path).parent.parent.name  # e.g. LIVE or PTU.
    comment_lines = [
        "---- Star Citizen game telemetry gRPC schema ----",
        "",
        f"What it is: the protobuf schema of the game's telemetry gRPC "
        f"API ({services}),",
        "including the telemetry frame messages it streams.",
        "",
        f"Compatible with: StarCitizen.exe file version {version} "
        f"({channel}).",
        "Other game versions may add, remove or change fields.",
        "",
        "Source: reconstructed from the descriptors embedded in "
        "StarCitizen.exe:",
        *(f"  {name}" for name in files),
        "Original source comments are not stored in the exe, so none "
        "are included.",
    ]
    return version, render_proto(files, comment_lines)


if __name__ == "__main__":
    print(f"Waiting for {EXE_NAME} to start...")
    while (exe := find_exe_path()) is None:
        time.sleep(CHECK_INTERVAL)
    file_version, proto_text = build(exe)
    if proto_text is None:
        print("No telemetry proto found")
    else:
        out = Path(__file__).parent / f"sc_telemetry_{file_version}.proto"
        out.write_text(proto_text, encoding="utf-8")
        print(f"Wrote {out}")
