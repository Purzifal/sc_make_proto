# sc_make_proto

Automatically generate the Star Citizen game telemetry `.proto` schema from your installed game.

`make_proto.py` waits for `StarCitizen.exe` to start. It then reads the protobuf schema of the game's telemetry gRPC API (`GameTelemetryService`) from the executable and writes it out as a single `.proto` file. The file builds on its own and is named after the game's file version:

```
sc_telemetry_4.10.201.48085.proto
```

Because the schema comes from whichever executable is running, you always get the version that matches your game build, LIVE or PTU.

## Compatibility

Currently compatible with Star Citizen **4.10.x** and **5.0.x**.

| Channel | Launcher build | StarCitizen.exe file version |
|---|---|---|
| LIVE | 4.10.3-live.13220821 | 4.10.201.48085 |
| PTU | 5.0.0-ptu.13248297 | 5.0.202.10025 |

> **Important:** for each environment (LIVE, PTU, ...), the game must be loaded to at least the **main menu** to build the proto schema for that environment.

The launcher build is the version shown in the RSI Launcher. The file version is the one read from `StarCitizen.exe` and used in the generated file name, e.g. `sc_telemetry_4.10.201.48085.proto`.

## Requirements

- Windows
- Python 3.10 or newer (tested with 3.15.2)
- The [`protobuf`](https://pypi.org/project/protobuf/) package
- Star Citizen installed

```
pip install -r requirements.txt
```

## Usage

```
python make_proto.py
```

1. Run the script. It prints `Waiting for StarCitizen.exe to start...`.
2. Launch Star Citizen, either LIVE or PTU, and let it load to at least the **main menu**. If the game is already running, the script continues straight away.
3. The script writes the `.proto` file next to itself and exits:

```
Waiting for StarCitizen.exe to start...
Wrote F:\...\sc_make_proto_auto\sc_telemetry_4.10.201.48085.proto
```

The `.proto` file is always written to the folder that contains `make_proto.py`, not to the game's folder (e.g. `...\StarCitizen\LIVE\Bin64\`). This is the same whichever folder you run the script from.

If a file with the same name already exists, it is overwritten.

## Output

The generated file starts with a header that says what it is and which game build it matches:

```proto
syntax = "proto3";
package sc.game.telemetry.v1;

// ---- Star Citizen game telemetry gRPC schema ----
//
// What it is: the protobuf schema of the game's telemetry gRPC API (GameTelemetryService),
// including the telemetry frame messages it streams.
//
// Compatible with: StarCitizen.exe file version 4.10.201.48085 (LIVE).
// Other game versions may add, remove or change fields.
// ...

message Availability { bool enabled = 1; bool active = 2; }
message Gauge { float current = 1; float max = 2; }

message VehicleDoorState {
  enum DoorOpenState { DOOR_OPEN_STATE_UNSPECIFIED = 0; DOOR_OPEN_STATE_OPENED = 1; DOOR_OPEN_STATE_CLOSED = 2; }
  enum DoorLockState { DOOR_LOCK_STATE_UNSPECIFIED = 0; DOOR_LOCK_STATE_UNLOCKED = 1; DOOR_LOCK_STATE_LOCKED = 2; }
  DoorOpenState open_state = 1;
  DoorLockState lock_state = 2;
}
...
service GameTelemetryService {
  rpc TelemetryStream(TelemetryStreamRequest) returns (stream TelemetryStreamResponse);
  rpc TriggerGameAction(TriggerGameActionRequest) returns (TriggerGameActionResponse);
  rpc ListTriggerableActions(ListTriggerableActionsRequest) returns (ListTriggerableActionsResponse);
}
```

The game ships the schema as two files, `sc/game/telemetry/v1/types.proto` and `sc/game/telemetry/v1/api.proto`. Both use the same package, so the script merges them into one file and drops the import between them.

## Building the schema

The generated file compiles with `protoc` as is. For example, using `grpcio-tools`:

```
pip install grpcio-tools
python -m grpc_tools.protoc -I. --descriptor_set_out=sc_telemetry.pb sc_telemetry_4.10.201.48085.proto
```

> **Note for Python code generation:** `protoc` names generated Python modules after the `.proto` file, e.g. `sc_telemetry_4.10.201.48085_pb2.py`. Python cannot import a module whose name contains dots, so copy or rename the file first, for example to `sc_telemetry.proto`:
>
> ```
> copy sc_telemetry_4.10.201.48085.proto sc_telemetry.proto
> python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. sc_telemetry.proto
> ```

## How it works

1. **Find the game:** it lists running processes using the Windows API (`CreateToolhelp32Snapshot`) and checks every 2 seconds until `StarCitizen.exe` appears.
2. **Find the exe:** it reads the full executable path of the process (`QueryFullProcessImageNameW`).
3. **Read the version:** it reads the numeric **File version** from the executable's version resource, the same value shown under *Properties > Details*.
4. **Find the schema:** it searches the executable for the embedded protobuf `FileDescriptorProto` data whose file names start with `sc/game/telemetry/`, and parses it with `protobuf`.
5. **Rebuild the `.proto`:** it merges the descriptors and turns them back into `.proto` source. Small definitions go on one line; larger ones span several lines.
6. **Write the file:** it saves the result as `sc_telemetry_{file_version}.proto`.

Apart from the `protobuf` package, it uses only the Python standard library (`ctypes`). It does not need admin rights. After the game is found, a run takes about 0.2 seconds.

## Limitations

- **No comments:** the original `.proto` comments are not stored in the executable, so the generated file only has its header comment.
- **Tied to one build:** each file matches the game build it was generated from. Generate it again after a patch.
- **First match only:** if more than one `StarCitizen.exe` is running, the first one found is used.
- **Windows only.**

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

## Disclaimer

This is an unofficial fan project and is not affiliated with or endorsed by Cloud Imperium Games. Star Citizen® is a registered trademark of Cloud Imperium Rights LLC. The script reads files from your own local game installation and does not connect to or modify the game.
