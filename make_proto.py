#!/usr/bin/env python3
"""
make_proto.py - dump the gRPC telemetry schema out of StarCitizen.exe and
write it as a .proto file.

WHY THIS EXISTS
    Star Citizen's built-in telemetry server (127.0.0.1:2942) has gRPC
    reflection switched off and CIG doesn't publish a .proto file. But the
    game was compiled with the full protobuf runtime, so the complete schema
    (every message, field name, field number, enum and rpc) is embedded in
    StarCitizen.exe as serialized "FileDescriptorProto" blobs. This script
    finds those blobs, decodes them, and writes them back out as ordinary
    .proto text that `protoc` / grpc_tools can compile.

USAGE
    python make_proto.py -<ENVIRONMENT> -<FILENAME> [options]

    -<ENVIRONMENT>   The game environment folder, e.g. -LIVE or -PTU.
                     Matched case-insensitively (-ptu finds the PTU folder).
    -<FILENAME>      Name for the output file. ".proto" is appended for you
                     (typing it yourself is fine, it won't be doubled).
                     Must not contain a path; use --out-dir for the folder.

    Options:
      --base DIR      Folder that contains the environment folders.
      --out-dir DIR   Where to write the .proto (default: the same folder
                      as this script, wherever you run it from).
      --match PREFIX  Which embedded .proto files to extract
                      (default: sc/game/telemetry/).
      -h, --help      Show this text.

EXAMPLES
    python make_proto.py -LIVE -telemetry_live
        reads  D:\\Roberts Space Industries\\StarCitizen\\LIVE\\Bin64\\StarCitizen.exe
        writes telemetry_live.proto next to make_proto.py

    python make_proto.py -PTU -telemetry_ptu
        reads  ...\\PTU\\Bin64\\StarCitizen.exe, writes .\\telemetry_ptu.proto

    python make_proto.py -LIVE -telemetry_live --base "E:\\Games\\StarCitizen"
        game installed somewhere else

    python make_proto.py -PTU -telemetry_ptu --out-dir protos
        write into a "protos" folder (created if missing)

THE EXE PATH
    {base}\\{environment}\\Bin64\\StarCitizen.exe

    The base folder is chosen in this order:
      1. --base "D:\\Roberts Space Industries\\StarCitizen"
      2. the SC_BASE_DIR environment variable
      3. DEFAULT_BASE below (edit it once and forget about it)

AFTER YOU HAVE THE .proto
    python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. telemetry_live.proto
    (this creates telemetry_live_pb2.py and telemetry_live_pb2_grpc.py)

REQUIREMENTS
    Python 3.8+ and the `protobuf` package (installed with grpcio-tools).
    No network access and no game process needed; it only reads the exe.
"""
import datetime
import mmap
import os
import re
import sys

# ----------------------------------------------------------------------------
# Settings you may want to edit
# ----------------------------------------------------------------------------

# Folder that holds the environment folders (LIVE, PTU, ...). Used when neither
# --base nor the SC_BASE_DIR environment variable is given.
DEFAULT_BASE = r"D:\Roberts Space Industries\StarCitizen"

# Only embedded .proto files whose name starts with this are extracted. The
# telemetry schema lives under sc/game/telemetry/ (types.proto and api.proto).
DEFAULT_MATCH = "sc/game/telemetry/"

# Folder this script lives in. Output goes here unless --out-dir is given, so
# the result lands next to make_proto.py no matter where you run it from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# What gets appended to {base}\{environment} to reach the executable.
EXE_TAIL = os.path.join("Bin64", "StarCitizen.exe")

try:
    from google.protobuf import descriptor_pb2
except ImportError:
    sys.exit("This needs the protobuf package:  pip install protobuf")

# Short alias; used a lot when rendering fields.
FDP = descriptor_pb2.FieldDescriptorProto


# ============================================================================
# 1. Command line handling
# ============================================================================
def die(msg):
    """Print an error to stderr and exit with a non-zero status."""
    print("error:", msg, file=sys.stderr)
    sys.exit(1)


def parse_cli(argv):
    """
    Parse the command line by hand (rather than argparse) so that the
    environment and filename can be given as single-dash words:

        -LIVE -telemetry_live

    argparse would treat those as unknown short flags. Rules used here:
      * tokens starting with "--" are options (--base X or --base=X)
      * every other token is positional; one leading "-" is stripped
      * exactly two positionals are required: environment, then filename

    Returns (environment, filename_without_extension, options_dict).
    """
    # Defaults for the options. The base folder falls back to SC_BASE_DIR,
    # then to DEFAULT_BASE.
    opts = {"base": os.environ.get("SC_BASE_DIR") or DEFAULT_BASE,
            "out_dir": SCRIPT_DIR,
            "match": DEFAULT_MATCH}
    pos = []  # positional words, in the order given
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help", "-?"):
            print(__doc__)
            sys.exit(0)
        if a.startswith("--"):
            # "--out-dir=foo" or "--out-dir foo"; dashes map to underscores
            key, eq, val = a[2:].partition("=")
            key = key.replace("-", "_")
            if key not in opts:
                die(f"unknown option {a}")
            if not eq:                      # value is the next token
                i += 1
                if i >= len(argv):
                    die(f"{a} needs a value")
                val = argv[i]
            opts[key] = val
        else:
            # "-LIVE" -> "LIVE"; a bare "LIVE" is accepted too
            pos.append(a[1:] if a.startswith("-") and len(a) > 1 else a)
        i += 1

    if len(pos) != 2:
        die("expected an environment and a filename, e.g.\n"
            "    python make_proto.py -LIVE -telemetry_live")
    env, name = pos

    # The filename is just a name; folders go through --out-dir. This also
    # stops things like "-..\\..\\x" from writing somewhere unexpected.
    if os.sep in name or "/" in name or "\\" in name:
        die("the filename can't contain a path; use --out-dir for the folder")
    if name.lower().endswith(".proto"):    # we append .proto ourselves
        name = name[:-6]
    if not name:
        die("empty filename")
    return env, name, opts


def find_exe(base, env):
    """
    Build {base}\\{env}\\Bin64\\StarCitizen.exe and check that it exists.

    The environment folder is matched case-insensitively, so "-ptu" works even
    though the folder is called "PTU" (matters on case-sensitive filesystems;
    harmless on Windows). If nothing matches, the error lists which
    environments under `base` do contain the exe, which usually shows the typo.
    """
    if not os.path.isdir(base):
        die(f"base folder not found: {base}\n"
            f"       set it with --base or SC_BASE_DIR")

    env_dir = os.path.join(base, env)
    if not os.path.isdir(env_dir):
        # Try a case-insensitive match against the folders in base
        for d in os.listdir(base):
            if d.lower() == env.lower() and os.path.isdir(os.path.join(base, d)):
                env_dir = os.path.join(base, d)
                break

    exe = os.path.join(env_dir, EXE_TAIL)
    if os.path.isfile(exe):
        return exe

    # Not found: tell the user what IS available
    avail = sorted(d for d in os.listdir(base)
                   if os.path.isfile(os.path.join(base, d, EXE_TAIL)))
    die(f"not found: {exe}\n"
        f"       environments with a Bin64\\StarCitizen.exe under {base}: "
        f"{', '.join(avail) if avail else 'none'}")


# ============================================================================
# 2. Pulling the serialized descriptors out of the exe
# ============================================================================
# A FileDescriptorProto is a protobuf message describing one .proto file. It is
# serialized in field-number order:
#     1 name, 2 package, 3 dependency[], 4 message_type[], 5 enum_type[],
#     6 service[], 8 options, 12 syntax
# The first field (name) is encoded as:   0x0A <length byte> <name bytes>
# so "0x0A 0x20 sc/game/telemetry/v1/types.proto" marks the start of one.
# That is what we search the exe for.

def varint(b, i):
    """
    Decode one protobuf varint from `b` starting at index `i`.
    Returns (value, next_index), or (None, i) if the bytes run out or the
    varint is longer than 10 bytes (so it can't be a real one).
    """
    v = s = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s          # low 7 bits carry data
        if not c & 0x80:              # high bit clear = last byte
            return v, i
        s += 7
        if s > 63:
            break
    return None, i


def descriptor_end(data, start):
    """
    Given the offset where a descriptor starts, walk its top-level fields to
    find where it ends. The exe doesn't tell us the length, and the bytes after
    the descriptor are unrelated, so we stop as soon as the next thing can't be
    a valid FileDescriptorProto field:

      * field number outside 1..12
      * field number going backwards (serialization is in ascending order)
      * a wire type other than varint (0) or length-delimited (2)
      * a length that runs past the end of the file
      * after field 12 (`syntax`), which is always serialized last in proto3

    Returns the offset just past the last valid field.
    """
    pos, last, n = start, 0, len(data)
    while pos < n:
        tag, p = varint(data, pos)
        if tag is None:
            break
        fn, wt = tag >> 3, tag & 7    # field number, wire type
        if fn < 1 or fn > 12 or fn < last or wt not in (0, 2):
            break
        if wt == 2:                   # length-delimited: skip over the payload
            ln, p = varint(data, p)
            if ln is None or p + ln > n:
                break
            p += ln
        else:                         # varint: skip the value
            v, p = varint(data, p)
            if v is None:
                break
        last, pos = fn, p
        if fn == 12:                  # `syntax` is last; nothing valid follows
            break
    return pos


def extract_descriptors(exe, match):
    """
    Scan `exe` for embedded descriptors whose file name starts with `match`
    and return them as a list of FileDescriptorProto objects.

    The exe is memory-mapped instead of read into RAM (it is hundreds of MB).
    Each hit is checked three ways before it's accepted: the length byte must
    equal the name length, the bytes must parse as a FileDescriptorProto, and
    the parsed name must equal the name we matched. Duplicate copies of the
    same file (the exe may contain several) are kept only once.
    """
    prefix = re.escape(match.encode())
    # 0x0A, one length byte (5..127), then a name that starts with the prefix
    # and ends in .proto. Group 1 = length byte, group 2 = the name.
    pat = re.compile(rb"\x0a([\x05-\x7f])(" + prefix + rb"[\w/.\-]*\.proto)")

    found = {}  # file name -> FileDescriptorProto (dict = de-duplication)
    with open(exe, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for m in pat.finditer(data):
            # Reject accidental matches: the length byte must be the real length
            if len(m.group(2)) != m.group(1)[0]:
                continue
            name = m.group(2).decode()
            if name in found:
                continue
            # Slice out exactly one descriptor and let protobuf parse it
            blob = data[m.start():descriptor_end(data, m.start())]
            fd = descriptor_pb2.FileDescriptorProto()
            try:
                fd.ParseFromString(blob)
            except Exception:
                continue                  # not a real descriptor
            # Final sanity checks: right file, and it actually defines something
            if fd.name != name or not (fd.message_type or fd.enum_type or fd.service):
                continue
            found[name] = fd
    return list(found.values())


def dependency_order(files):
    """
    Sort files so each one comes after the files it imports (api.proto imports
    types.proto, so types.proto must come first in the merged output).
    Simple depth-first topological sort.
    """
    by = {f.name: f for f in files}
    out, seen = [], set()

    def visit(f):
        if f.name in seen:
            return
        seen.add(f.name)
        for d in f.dependency:
            if d in by:               # only follow dependencies we extracted
                visit(by[d])
        out.append(f)                 # added after its dependencies

    for f in files:
        visit(f)
    return out


# ============================================================================
# 3. Turning FileDescriptorProtos back into .proto text
# ============================================================================
class Writer:
    """
    Renders one or more FileDescriptorProtos as a single merged .proto file.

    Everything is merged into one file, so references between the files
    (api.proto using types from types.proto) no longer need imports. Type names
    are written relative to the package (".sc.game.telemetry.v1.Gauge" becomes
    "Gauge"), which is valid because everything is in the same package.
    """

    def __init__(self, files):
        self.files = files
        pkgs = [f.package for f in files]
        self.pkg = pkgs[0]
        if len(set(pkgs)) > 1:
            print(f"warning: several packages found {sorted(set(pkgs))}; "
                  f"only '{self.pkg}' is merged cleanly", file=sys.stderr)
        self.proto3 = files[0].syntax == "proto3"
        # Imports the merged file still needs: dependencies that are NOT among
        # the files we extracted (e.g. google/protobuf/empty.proto). Our own
        # files are merged in, so importing them would be circular.
        names = {f.name for f in files}
        self.imports = sorted({d for f in files for d in f.dependency} - names)

    # ---- type names --------------------------------------------------------
    def tname(self, t):
        """'.sc.game.telemetry.v1.Gauge' -> 'Gauge' (strip package + leading dot)."""
        pre = "." + self.pkg + "." if self.pkg else "."
        return t[len(pre):] if t.startswith(pre) else t.lstrip(".")

    def ftype(self, f):
        """Type text for a field: a message/enum name, or a scalar like 'float'."""
        if f.type in (FDP.TYPE_MESSAGE, FDP.TYPE_ENUM, FDP.TYPE_GROUP):
            return self.tname(f.type_name)
        return FDP.Type.Name(f.type)[5:].lower()    # TYPE_FLOAT -> float

    # ---- enums -------------------------------------------------------------
    def enum(self, e, ind):
        """Render an enum (top-level or nested) at indent level `ind`."""
        p = "  " * ind
        out = [f"{p}enum {e.name} {{"]
        if e.options.allow_alias:
            out.append(f"{p}  option allow_alias = true;")
        out += [f"{p}  {v.name} = {v.number};" for v in e.value]
        out.append(f"{p}}}")
        return out

    @staticmethod
    def ranges(rs):
        """Format reserved ranges like '2, 5 to 9'. (End values are exclusive.)"""
        parts = []
        for r in rs:
            end = r.end - 1
            end_s = "max" if end >= 536870911 else str(end)
            parts.append(str(r.start) if end == r.start else f"{r.start} to {end_s}")
        return ", ".join(parts)

    # ---- messages ----------------------------------------------------------
    def message(self, m, ind):
        """
        Render a message, including nested messages/enums, oneofs, map fields
        and reserved ranges. Returns a list of lines.
        """
        p = "  " * ind
        # Map fields are stored as a hidden nested "XxxEntry" message flagged
        # map_entry. Collect them so we can print `map<K, V>` and not emit the
        # hidden message itself.
        maps = {n.name: n for n in m.nested_type if n.options.map_entry}
        out = [f"{p}message {m.name} {{"]
        done_oneofs = set()

        def field_line(f, q):
            """One field: handles map<>, repeated, optional/required labels."""
            typ = self.ftype(f)
            # A repeated field whose type is a hidden *Entry message is a map
            entry = (maps.get(f.type_name.rsplit(".", 1)[-1])
                     if f.type == FDP.TYPE_MESSAGE else None)
            if entry is not None and f.label == FDP.LABEL_REPEATED:
                k, v = entry.field[0], entry.field[1]   # key=1, value=2
                return f"{q}map<{self.ftype(k)}, {self.ftype(v)}> {f.name} = {f.number};"
            if f.label == FDP.LABEL_REPEATED:
                lab = "repeated "
            elif f.proto3_optional:                     # explicit `optional` in proto3
                lab = "optional "
            elif not self.proto3 and f.label == FDP.LABEL_REQUIRED:
                lab = "required "
            elif not self.proto3:                       # proto2 needs a label on everything
                lab = "optional "
            else:                                       # plain proto3 field: no label
                lab = ""
            return f"{q}{lab}{typ} {f.name} = {f.number};"

        for f in m.field:
            if f.HasField("oneof_index") and not f.proto3_optional:
                # Part of a real oneof: print the whole group the first time
                # we meet any member, in place of that member.
                oi = f.oneof_index
                if oi in done_oneofs:
                    continue
                done_oneofs.add(oi)
                out.append(f"{p}  oneof {m.oneof_decl[oi].name} {{")
                for g in m.field:
                    if g.HasField("oneof_index") and g.oneof_index == oi:
                        out.append(field_line(g, p + "    ").replace("optional ", "", 1))
                out.append(f"{p}  }}")
            else:
                out.append(field_line(f, p + "  "))

        if m.reserved_range:
            out.append(f"{p}  reserved {self.ranges(m.reserved_range)};")
        if m.reserved_name:
            out.append(f"{p}  reserved " + ", ".join(f'"{n}"' for n in m.reserved_name) + ";")
        for e in m.enum_type:                    # nested enums
            out += self.enum(e, ind + 1)
        for n in m.nested_type:                  # nested messages (not map entries)
            if n.name not in maps:
                out += self.message(n, ind + 1)
        out.append(f"{p}}}")
        return out

    # ---- services ----------------------------------------------------------
    def service(self, s):
        """Render a service and its rpc methods, keeping `stream` markers."""
        out = [f"service {s.name} {{"]
        for m in s.method:
            cs = "stream " if m.client_streaming else ""
            ss = "stream " if m.server_streaming else ""
            out.append(f"  rpc {m.name}({cs}{self.tname(m.input_type)}) "
                       f"returns ({ss}{self.tname(m.output_type)});")
        out.append("}")
        return out

    # ---- whole file --------------------------------------------------------
    def render(self, header_lines):
        """Produce the full .proto text: header, syntax, package, then each
        source file's enums, messages and services in dependency order."""
        out = [f"// {h}" for h in header_lines]
        out.append("")
        out.append('syntax = "proto3";' if self.proto3 else 'syntax = "proto2";')
        if self.pkg:
            out.append(f"package {self.pkg};")
        for imp in self.imports:
            out.append(f'import "{imp}";')
        for f in self.files:
            out.append("")
            out.append(f"// ---- from {f.name} ----")
            for e in f.enum_type:
                out.append("")
                out += self.enum(e, 0)
            for m in f.message_type:
                out.append("")
                out += self.message(m, 0)
            for s in f.service:
                out.append("")
                out += self.service(s)
        return "\n".join(out) + "\n"

    # ---- sanity check ------------------------------------------------------
    def unresolved(self):
        """
        Return every type that is referenced (by a field or rpc) but not
        defined in the extracted files. An empty list means the output is
        self-contained. Types from google.protobuf.* are ignored since those
        come from standard imports.
        """
        defined = set()

        def walk(prefix, msgs, enums):
            """Collect the full name of every enum and message, nested included."""
            for e in enums:
                defined.add(f"{prefix}.{e.name}")
            for m in msgs:
                full = f"{prefix}.{m.name}"
                defined.add(full)
                walk(full, m.nested_type, m.enum_type)

        refs = set()

        def refwalk(msgs):
            """Collect every type name that a field refers to."""
            for m in msgs:
                for f in m.field:
                    if f.type_name:
                        refs.add(f.type_name)
                refwalk(m.nested_type)

        for f in self.files:
            walk("." + f.package, f.message_type, f.enum_type)
            refwalk(f.message_type)
            for s in f.service:
                for m in s.method:
                    refs.update((m.input_type, m.output_type))
        return sorted(r for r in refs
                      if r not in defined and not r.startswith(".google.protobuf."))


# ============================================================================
# 4. Main
# ============================================================================
def count_messages(msgs):
    """Count messages including nested ones (used for the summary line)."""
    return sum(1 + count_messages(m.nested_type) for m in msgs)


def main():
    # 1. Work out which exe to read and what to call the output
    env, name, opts = parse_cli(sys.argv[1:])
    exe = find_exe(opts["base"], env)
    print(f"reading {exe}")

    # 2. Pull the embedded descriptors out of the exe
    files = extract_descriptors(exe, opts["match"])
    if not files:
        die(f"no embedded '{opts['match']}*.proto' descriptors found in that exe.\n"
            f"       The build may have stripped them (lite runtime), or the path prefix\n"
            f"       differs; try --match with a different prefix.")
    files = dependency_order(files)

    # 3. Check the dump is self-contained; warn (don't fail) if not
    w = Writer(files)
    bad = w.unresolved()
    if bad:
        print("warning: types referenced but not defined in the dumped files:", file=sys.stderr)
        for b in bad:
            print("   ", b, file=sys.stderr)

    # 4. Render and write the .proto. The header names the environment folder
    #    that was actually read, plus the date, so old dumps can be told apart.
    header = [f"Dumped from the {os.path.basename(os.path.dirname(os.path.dirname(exe)))} build"
              f" - {datetime.date.today().isoformat()}",
              "Reconstructed from the descriptor embedded in StarCitizen.exe."]
    text = w.render(header)

    os.makedirs(opts["out_dir"], exist_ok=True)
    out_path = os.path.join(opts["out_dir"], name + ".proto")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    # 5. Summary
    nmsg = sum(count_messages(f.message_type) for f in files)
    nsvc = [f"{s.name} ({len(s.method)} rpc)" for f in files for s in f.service]
    print(f"found {len(files)} files: " + ", ".join(f.name for f in files))
    print(f"{nmsg} messages; services: {', '.join(nsvc) or 'none'}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
