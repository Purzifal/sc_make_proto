# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the Star Citizen telemetry .proto schema from the game.

Waits for StarCitizen.exe to start, reads its file version, extracts
the embedded gRPC telemetry protobuf descriptors from the executable,
and writes them as a single buildable ``sc_telemetry_{version}.proto``
file next to this script.
"""

import ctypes
import mmap
import time
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path

from google.protobuf import descriptor_pb2

EXE_NAME = "StarCitizen.exe"
CHECK_INTERVAL = 2  # Seconds between checks.

# An embedded .proto schema starts with its file name: byte 0x0A, a
# one-byte name length, then the name. Telemetry schemas live under
# this prefix.
_TELEMETRY_PREFIX = b"sc/game/telemetry/"

# Signature that marks a valid VS_FIXEDFILEINFO structure.
_VS_FFI_SIGNATURE = 0xFEEF04BD

# FileDescriptorProto field number -> allowed protobuf wire types. Used
# to find where an embedded descriptor ends.
_DESCRIPTOR_FIELDS = {
    1: {2}, 2: {2}, 3: {2}, 4: {2}, 5: {2}, 6: {2}, 7: {2}, 8: {2},
    9: {2}, 10: {0, 2}, 11: {0, 2}, 12: {2}, 14: {0},
}

FDP = descriptor_pb2.FieldDescriptorProto

# A definition is printed on one line if it has no nested definitions
# and fits within this many characters.
LINE_LIMIT = 120

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class PROCESSENTRY32W(ctypes.Structure):
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


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32FirstW.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESSENTRY32W),
]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESSENTRY32W),
]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_version = ctypes.WinDLL("version", use_last_error=True)
_version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
_version.GetFileVersionInfoSizeW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_version.GetFileVersionInfoW.restype = wintypes.BOOL
_version.GetFileVersionInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_version.VerQueryValueW.restype = wintypes.BOOL
_version.VerQueryValueW.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.UINT),
]


class VS_FIXEDFILEINFO(ctypes.Structure):
    """Mirror of the Win32 VS_FIXEDFILEINFO structure."""

    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def find_pid(exe_name: str = EXE_NAME) -> int | None:
    """Return the PID of the named process, or None if it is not running.

    Args:
        exe_name: The process image name to look for.

    Returns:
        The process ID, or None if no matching process is running.
    """
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        target = exe_name.casefold()
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        entry_ref = ctypes.byref(entry)
        found = _kernel32.Process32FirstW(snapshot, entry_ref)
        while found:
            if entry.szExeFile.casefold() == target:
                return entry.th32ProcessID
            found = _kernel32.Process32NextW(snapshot, entry_ref)
        return None
    finally:
        _kernel32.CloseHandle(snapshot)


def get_exe_path(pid: int) -> str | None:
    """Return the full path of the executable for a process.

    Args:
        pid: The process ID.

    Returns:
        The full executable path, or None if it cannot be read.
    """
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return buffer.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def get_file_version(path: str) -> str | None:
    """Return the "File version" shown in the file's Properties > Details.

    Args:
        path: Full path to the executable.

    Returns:
        The file version as "a.b.c.d", or None if it cannot be read.
    """
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, data):
        return None

    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not _version.VerQueryValueW(
        data, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return None
    if not pointer.value or length.value < ctypes.sizeof(VS_FIXEDFILEINFO):
        return None

    info = VS_FIXEDFILEINFO.from_address(pointer.value)
    if info.dwSignature != _VS_FFI_SIGNATURE:
        return None
    ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _read_varint(data: bytes | mmap.mmap, i: int) -> tuple[int, int]:
    """Decode a protobuf varint.

    Args:
        data: The bytes to read from.
        i: Offset of the varint's first byte.

    Returns:
        The decoded value and the offset just past it.
    """
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _descriptor_end(data: bytes | mmap.mmap, start: int) -> int:
    """Return the offset where an embedded descriptor ends.

    Reads protobuf fields from ``start`` until one appears that cannot
    belong to a FileDescriptorProto.

    Args:
        data: The executable's bytes.
        start: Offset of the descriptor's first byte.

    Returns:
        The offset just past the descriptor's last field.
    """
    i = start
    size = len(data)
    while i < size:
        try:
            tag, j = _read_varint(data, i)
            wire_type = tag & 7
            if wire_type not in _DESCRIPTOR_FIELDS.get(tag >> 3, ()):
                break
            if wire_type == 0:
                _, j = _read_varint(data, j)
            else:
                length, j = _read_varint(data, j)
                j += length
        except IndexError:
            break
        if j > size:
            break
        i = j
    return i


def _iter_telemetry_names(
    data: bytes | mmap.mmap,
) -> Iterator[tuple[int, str]]:
    """Yield the location and name of each embedded telemetry schema.

    A schema's file name is stored as byte 0x0A, a one-byte length and
    then the name itself, so a match on the telemetry prefix is only
    accepted if those two bytes come right before it.

    Args:
        data: The executable's bytes.

    Yields:
        The offset where the descriptor starts and its .proto file name.
    """
    hit = data.find(_TELEMETRY_PREFIX)
    while hit != -1:
        start = hit - 2
        if start >= 0 and data[start] == 0x0A and data[hit - 1] < 0x80:
            raw = data[hit:hit + data[hit - 1]]
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                name = ""
            if name.endswith(".proto") and name.isprintable():
                yield start, name
        hit = data.find(_TELEMETRY_PREFIX, hit + 1)


def find_telemetry_descriptors(
    path: str,
) -> dict[str, descriptor_pb2.FileDescriptorProto]:
    """Return the telemetry .proto descriptors embedded in a file.

    The file is memory-mapped rather than read into memory, so only the
    pages the search touches are loaded and nothing has to be copied.

    Args:
        path: Full path to the executable.

    Returns:
        A mapping of .proto file name to its descriptor, in the order
        they appear. The mapping is empty if none are found.
    """
    descriptors: dict[str, descriptor_pb2.FileDescriptorProto] = {}
    with (
        open(path, "rb") as file,
        mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        for start, name in _iter_telemetry_names(data):
            if name in descriptors:
                continue
            descriptor = descriptor_pb2.FileDescriptorProto()
            try:
                descriptor.ParseFromString(
                    data[start:_descriptor_end(data, start)]
                )
            except Exception:
                continue
            if (
                descriptor.message_type
                or descriptor.enum_type
                or descriptor.service
            ):
                descriptors[name] = descriptor
    return descriptors


def _quote(text: str) -> str:
    """Return ``text`` as a double-quoted .proto string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _options(options) -> list[str]:
    """Return ``name = value`` strings for every option that is set.

    Args:
        options: A protobuf options message (file, message, field, ...).

    Returns:
        One ``name = value`` string per option. The internal
        ``map_entry`` option is left out.
    """
    result = []
    for field, value in options.ListFields():
        if field.name == "map_entry":
            continue
        if field.enum_type is not None:
            text = field.enum_type.values_by_number[value].name
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, str):
            text = _quote(value)
        else:
            text = str(value)
        result.append(f"{field.name} = {text}")
    return result


def _default_json_name(name: str) -> str:
    """Return the JSON name protoc generates by default for a field."""
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _short_type(type_name: str, package: str, scope: list[str]) -> str:
    """Shorten a fully-qualified type name for use inside a definition.

    Args:
        type_name: The fully-qualified name, e.g. ``.pkg.Outer.Inner``.
        package: The package of the file being printed.
        scope: Names of the enclosing messages, outermost first.

    Returns:
        The shortest name that still refers to the same type.
    """
    name = type_name.lstrip(".")
    if package and name.startswith(package + "."):
        name = name[len(package) + 1:]
    for depth in range(len(scope), 0, -1):
        prefix = ".".join(scope[:depth]) + "."
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _field_type(field, package: str, scope: list[str]) -> str:
    """Return the .proto type name of a field."""
    if field.type in (FDP.TYPE_MESSAGE, FDP.TYPE_ENUM, FDP.TYPE_GROUP):
        return _short_type(field.type_name, package, scope)
    return FDP.Type.Name(field.type)[len("TYPE_"):].lower()


def _field_statement(
    field, package: str, scope: list[str], map_entries: dict, syntax: str,
    in_oneof: bool = False,
) -> str:
    """Return a field definition, e.g. ``repeated string names = 3;``."""
    entry = None
    if field.label == FDP.LABEL_REPEATED and field.type == FDP.TYPE_MESSAGE:
        entry = map_entries.get(field.type_name.rsplit(".", 1)[-1])

    if entry is not None:
        key_type = _field_type(entry.field[0], package, scope)
        value_type = _field_type(entry.field[1], package, scope)
        prefix = f"map<{key_type}, {value_type}>"
    else:
        label = ""
        if field.label == FDP.LABEL_REPEATED:
            label = "repeated "
        elif field.proto3_optional:
            label = "optional "
        elif field.label == FDP.LABEL_REQUIRED:
            label = "required "
        elif syntax != "proto3" and not in_oneof:
            label = "optional "
        prefix = label + _field_type(field, package, scope)

    options = []
    if field.HasField("default_value"):
        value = field.default_value
        if field.type in (FDP.TYPE_STRING, FDP.TYPE_BYTES):
            value = _quote(value)
        options.append(f"default = {value}")
    if field.HasField("json_name") and (
        field.json_name != _default_json_name(field.name)
    ):
        options.append(f"json_name = {_quote(field.json_name)}")
    if field.HasField("options"):
        options.extend(_options(field.options))
    suffix = f" [{', '.join(options)}]" if options else ""
    return f"{prefix} {field.name} = {field.number}{suffix};"


def _reserved_statements(ranges, names, inclusive_end: bool) -> list[str]:
    """Return ``reserved`` statements for numbers and names."""
    statements = []
    parts = []
    for r in ranges:
        end = r.end if inclusive_end else r.end - 1
        parts.append(str(r.start) if r.start == end else f"{r.start} to {end}")
    if parts:
        statements.append(f"reserved {', '.join(parts)};")
    if names:
        statements.append(f"reserved {', '.join(_quote(n) for n in names)};")
    return statements


def _block(depth: int, head: str, items: list) -> list[str]:
    """Render a ``head { ... }`` block as indented lines.

    Args:
        depth: Indentation level of the block.
        head: The text before the brace, e.g. ``message Gauge``.
        items: The block's contents, in order. Each item is either a
            statement string or a list of already-rendered lines for a
            nested block.

    Returns:
        A single line if the block has no nested blocks and fits within
        ``LINE_LIMIT``, otherwise one line per statement.
    """
    pad = "  " * depth
    if all(isinstance(item, str) for item in items):
        if not items:
            return [f"{pad}{head} {{}}"]
        line = f"{pad}{head} {{ {' '.join(items)} }}"
        if len(line) <= LINE_LIMIT:
            return [line]
    lines = [f"{pad}{head} {{"]
    for item in items:
        if isinstance(item, str):
            lines.append(f"{pad}  {item}")
        else:
            lines.extend(item)
    lines.append(f"{pad}}}")
    return lines


def _render_enum(enum, depth: int) -> list[str]:
    """Render an enum definition as indented lines."""
    items: list = []
    if enum.HasField("options"):
        items.extend(f"option {o};" for o in _options(enum.options))
    for value in enum.value:
        options = _options(value.options) if value.HasField("options") else []
        suffix = f" [{', '.join(options)}]" if options else ""
        items.append(f"{value.name} = {value.number}{suffix};")
    items.extend(_reserved_statements(
        enum.reserved_range, enum.reserved_name, inclusive_end=True,
    ))
    return _block(depth, f"enum {enum.name}", items)


def _render_message(
    message, depth: int, package: str, scope: list[str], syntax: str,
) -> list[str]:
    """Render a message definition, including nested types, as lines."""
    inner_scope = scope + [message.name]
    map_entries = {
        nested.name: nested
        for nested in message.nested_type
        if nested.options.map_entry
    }
    items: list = []
    if message.HasField("options"):
        items.extend(f"option {o};" for o in _options(message.options))
    for enum in message.enum_type:
        items.append(_render_enum(enum, depth + 1))
    for nested in message.nested_type:
        if nested.name not in map_entries:
            items.append(_render_message(
                nested, depth + 1, package, inner_scope, syntax,
            ))

    # Oneofs created only to support proto3 "optional" are not printed.
    real_oneofs = {
        field.oneof_index
        for field in message.field
        if field.HasField("oneof_index") and not field.proto3_optional
    }
    printed_oneofs: set[int] = set()
    for field in message.field:
        index = field.oneof_index if field.HasField("oneof_index") else None
        if index not in real_oneofs:
            items.append(_field_statement(
                field, package, inner_scope, map_entries, syntax,
            ))
            continue
        if index in printed_oneofs:
            continue
        printed_oneofs.add(index)
        members = [
            _field_statement(
                member, package, inner_scope, map_entries, syntax,
                in_oneof=True,
            )
            for member in message.field
            if member.HasField("oneof_index") and member.oneof_index == index
        ]
        items.append(_block(
            depth + 1, f"oneof {message.oneof_decl[index].name}", members,
        ))

    items.extend(_reserved_statements(
        message.reserved_range, message.reserved_name, inclusive_end=False,
    ))
    return _block(depth, f"message {message.name}", items)


def _render_service(service, package: str) -> list[str]:
    """Render a gRPC service definition as lines."""
    items: list = []
    if service.HasField("options"):
        items.extend(f"option {o};" for o in _options(service.options))
    for method in service.method:
        request = _short_type(method.input_type, package, [])
        response = _short_type(method.output_type, package, [])
        if method.client_streaming:
            request = f"stream {request}"
        if method.server_streaming:
            response = f"stream {response}"
        options = (
            _options(method.options) if method.HasField("options") else []
        )
        body = (
            " { " + " ".join(f"option {o};" for o in options) + " }"
            if options else ";"
        )
        items.append(
            f"rpc {method.name}({request}) returns ({response}){body}"
        )
    return _block(0, f"service {service.name}", items)


def render_proto(
    descriptor: descriptor_pb2.FileDescriptorProto, comment_lines: list[str],
) -> str:
    """Rebuild .proto source text from a file descriptor.

    Args:
        descriptor: The file descriptor to render.
        comment_lines: Lines of the comment block printed under the
            header. Each line is prefixed with ``//``.

    Returns:
        The .proto source. Comments from the original source are not
        included because they are not stored in the descriptor.
    """
    package = descriptor.package
    syntax = descriptor.syntax or "proto2"
    lines = [f'syntax = "{syntax}";']
    if package:
        lines.append(f"package {package};")
    public = set(descriptor.public_dependency)
    weak = set(descriptor.weak_dependency)
    for index, dependency in enumerate(descriptor.dependency):
        modifier = (
            "public " if index in public else "weak " if index in weak else ""
        )
        lines.append(f"import {modifier}{_quote(dependency)};")
    lines.append("")
    lines.extend(f"// {line}".rstrip() for line in comment_lines)
    lines.append("")
    if descriptor.HasField("options"):
        lines.extend(f"option {o};" for o in _options(descriptor.options))
        lines.append("")

    blocks = [_render_enum(enum, 0) for enum in descriptor.enum_type]
    blocks += [
        _render_message(message, 0, package, [], syntax)
        for message in descriptor.message_type
    ]
    blocks += [
        _render_service(service, package) for service in descriptor.service
    ]

    # Consecutive one-line definitions sit together; a blank line
    # separates anything that spans several lines.
    previous: list[str] | None = None
    for block in blocks:
        if previous is not None and (len(previous) > 1 or len(block) > 1):
            lines.append("")
        lines.extend(block)
        previous = block
    return "\n".join(lines) + "\n"


def merge_descriptors(
    descriptors: dict[str, descriptor_pb2.FileDescriptorProto], name: str,
) -> descriptor_pb2.FileDescriptorProto:
    """Combine several .proto file descriptors into one.

    Imports between the combined files are dropped, because their
    definitions are now in the same file. All files must share the same
    package.

    Args:
        descriptors: The descriptors to combine, keyed by file name.
        name: File name for the combined descriptor.

    Returns:
        A single descriptor containing every definition.

    Raises:
        ValueError: If the files use different packages.
    """
    packages = {d.package for d in descriptors.values()}
    if len(packages) > 1:
        raise ValueError(f"Cannot merge different packages: {packages}")

    merged = descriptor_pb2.FileDescriptorProto(name=name)
    if packages:
        merged.package = packages.pop()
    for descriptor in descriptors.values():
        if descriptor.syntax:
            merged.syntax = descriptor.syntax
        if descriptor.HasField("options") and not merged.HasField("options"):
            merged.options.CopyFrom(descriptor.options)
        merged.message_type.extend(descriptor.message_type)
        merged.enum_type.extend(descriptor.enum_type)
        merged.service.extend(descriptor.service)
        for dependency in descriptor.dependency:
            if dependency not in descriptors and (
                dependency not in merged.dependency
            ):
                merged.dependency.append(dependency)
    return merged


def wait_for_process(exe_name: str = EXE_NAME) -> int:
    """Block until the named process is running and return its PID.

    Args:
        exe_name: The process image name to wait for.

    Returns:
        The process ID once the process is running.
    """
    print(f"Waiting for {exe_name} to start...")
    while (pid := find_pid(exe_name)) is None:
        time.sleep(CHECK_INTERVAL)
    return pid


if __name__ == "__main__":
    pid = wait_for_process()
    exe_path = get_exe_path(pid)
    file_version = get_file_version(exe_path) if exe_path else None
    telemetry_descriptors = (
        find_telemetry_descriptors(exe_path) if exe_path else {}
    )
    if telemetry_descriptors:
        channel = Path(exe_path).parent.parent.name
        output_path = (
            Path(__file__).parent / f"sc_telemetry_{file_version}.proto"
        )
        merged = merge_descriptors(telemetry_descriptors, output_path.name)
        services = ", ".join(service.name for service in merged.service)
        comment_lines = [
            "---- Star Citizen game telemetry gRPC schema ----",
            "",
            f"What it is: the protobuf schema of the game's telemetry gRPC "
            f"API ({services}),",
            "including the telemetry frame messages it streams.",
            "",
            f"Compatible with: StarCitizen.exe file version {file_version} "
            f"({channel}).",
            "Other game versions may add, remove or change fields.",
            "",
            "Source: reconstructed from the descriptors embedded in "
            "StarCitizen.exe:",
            *(f"  {name}" for name in telemetry_descriptors),
            "Original source comments are not stored in the exe, so none "
            "are included.",
        ]
        proto_text = render_proto(merged, comment_lines)
        output_path.write_text(proto_text, encoding="utf-8")
        print(f"Wrote {output_path}")
    else:
        print("No telemetry proto found")
