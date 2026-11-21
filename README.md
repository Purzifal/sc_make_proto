# make_proto.py

Dumps the gRPC telemetry schema out of `StarCitizen.exe` and writes it as a normal `.proto` file.

Star Citizen's built-in telemetry server has gRPC reflection switched off and CIG doesn't publish a `.proto`. The game does embed the full schema in the executable (every message, field name, field number, enum and rpc), so this script finds it, decodes it and writes it back out as `.proto` text that `protoc` can compile.

> Unofficial and undocumented. CIG can change or remove any of this in a patch, which is why the script reads the schema from the exe instead of hardcoding it. Re-run it after each patch.

## Requirements

- Python 3.8+
- The `protobuf` package (installed automatically with `grpcio-tools`)

```
pip install -r requirements.txt
```

`make_proto.py` itself only needs `protobuf`. `grpcio` and `grpcio-tools` are for compiling the result and reading the stream.

The script only reads the exe. It doesn't need the game running, the telemetry server enabled, or a network connection.

## Quick start

```
python make_proto.py -LIVE -telemetry_live
```

This reads:

```
D:\Roberts Space Industries\StarCitizen\LIVE\Bin64\StarCitizen.exe
```

and writes `telemetry_live.proto` in the same folder as `make_proto.py`, wherever you run the command from.

## Usage

```
python make_proto.py -<ENVIRONMENT> -<FILENAME> [--base DIR] [--out-dir DIR] [--match PREFIX]
```

| Argument | Meaning |
|---|---|
| `-<ENVIRONMENT>` | Environment folder name, such as `-LIVE` or `-PTU`. Matched case-insensitively, so `-ptu` finds the `PTU` folder. |
| `-<FILENAME>` | Output filename. `.proto` is appended for you. Typing `.proto` yourself is fine; it isn't doubled. Must not contain a path. |
| `--base DIR` | Folder that contains the environment folders. See below for the default. |
| `--out-dir DIR` | Where to write the file. Defaults to the same folder as the script, and is created if missing. A relative path is relative to the folder you run the command from. |
| `--match PREFIX` | Which embedded `.proto` files to extract. Default `sc/game/telemetry/`. |
| `-h`, `--help` | Show the built-in help. |

The environment and filename are the two dashed words, in that order.

### Examples

```
python make_proto.py -LIVE -telemetry_live
python make_proto.py -PTU -telemetry_ptu
python make_proto.py -LIVE -telemetry_live --base "E:\Games\StarCitizen"
python make_proto.py -PTU -telemetry_ptu --out-dir protos
```

## How the exe path is built

```
{base}\{environment}\Bin64\StarCitizen.exe
```

The base folder is taken from the first of these that is set:

1. `--base "D:\Roberts Space Industries\StarCitizen"`
2. The `SC_BASE_DIR` environment variable
3. `DEFAULT_BASE` at the top of `make_proto.py`

To set it once and forget about it, either edit `DEFAULT_BASE` or run:

```
setx SC_BASE_DIR "D:\Roberts Space Industries\StarCitizen"
```

(Open a new terminal afterwards so the variable is picked up.)

## What you get

A single merged `.proto` containing everything under `sc/game/telemetry/` (currently `types.proto` and `api.proto`), with dependencies first:

- `syntax`, `package`, all messages and enums with their real field numbers
- the `GameTelemetryService` definition, including `stream` markers

The script prints a summary when it finishes:

```
reading D:\Roberts Space Industries\StarCitizen\LIVE\Bin64\StarCitizen.exe
found 2 files: sc/game/telemetry/v1/types.proto, sc/game/telemetry/v1/api.proto
28 messages; services: GameTelemetryService (1 rpc)
wrote telemetry_live.proto
```

It also checks that every type the output refers to is defined in it, and prints a warning if not.

## Using the result

Compile it:

```
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. telemetry_live.proto
```

This creates `telemetry_live_pb2.py` and `telemetry_live_pb2_grpc.py`. Then read the stream:

```python
import grpc, time
import telemetry_live_pb2 as pb
import telemetry_live_pb2_grpc as pbg

while True:
    try:
        ch = grpc.insecure_channel("127.0.0.1:2942")
        stub = pbg.GameTelemetryServiceStub(ch)
        for r in stub.TelemetryStream(pb.TelemetryStreamRequest()):
            print(r.actor.vitals.health.current, r.actor.state.uec_balance)
    except grpc.RpcError:
        time.sleep(3)
```

The server only exists when the game was started with the environment variable set:

```
setx GRPC_CLIENT_SERVER_ENABLED true
```

Then fully exit the RSI launcher (system tray) and restart it. The game listens on `127.0.0.1:2942`.

### LIVE and PTU together

Both builds use the same package and message names, so their generated Python modules would clash if loaded together. If you keep both, generate and compile each in its own folder:

```
python make_proto.py -LIVE -telemetry --out-dir live
python make_proto.py -PTU  -telemetry --out-dir ptu
```

PTU is a superset of LIVE at the time of writing. It adds vehicle `shields`, `health` and `master_mode`, plus the `TriggerGameAction` and `ListTriggerableActions` methods. `TriggerGameAction` **sends input to the game** instead of just reading data, so test it carefully and check the anti-cheat and terms-of-service side first. A method that exists in the PTU proto will return `UNIMPLEMENTED` on a LIVE server that doesn't have it yet.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `base folder not found` | The base path is wrong. Pass `--base` or set `SC_BASE_DIR`. |
| `not found: ...StarCitizen.exe` | The environment folder doesn't exist or has no `Bin64\StarCitizen.exe`. The error lists the environments it did find. |
| `expected an environment and a filename` | You need exactly two dashed words, e.g. `-LIVE -telemetry_live`. |
| `the filename can't contain a path` | Use `--out-dir` for the folder. |
| `no embedded ... descriptors found` | The build may have stripped the schema, or the path prefix changed. Try `--match` with a different prefix. |
| `warning: types referenced but not defined` | The dump is incomplete, which can happen if a descriptor was cut short. Report which types were listed. |
| `This needs the protobuf package` | `pip install protobuf` (or `grpcio-tools`). |

## How it works

1. A serialized `.proto` description starts with the bytes `0x0A <length> <file name>`, so the script scans the exe for those, using a memory map so the large file isn't loaded into RAM.
2. For each hit it walks the top-level fields to find where the descriptor ends, then lets `protobuf` parse it. Hits that fail the length check or don't parse are skipped, and duplicate copies are kept once.
3. The descriptors are sorted so dependencies come first and rendered as `.proto` text, handling nested types, oneofs, maps, reserved ranges and streaming rpcs.

## Limitations

- Only tested against synthetic `StarCitizen.exe` files built from the real LIVE and PTU schemas. If your real exe behaves differently, such as finding nothing, that's worth reporting.
- It recovers names and field numbers, but not units or meanings. For example, altitude and heading have no unit in the schema.
- The output isn't guaranteed to be byte-for-byte what CIG wrote; it's an equivalent reconstruction. Comments and original formatting aren't stored in the exe.
- Output is valid proto3, but nested references appear fully qualified (`VehicleDoorState.DoorOpenState`) rather than shortened.
