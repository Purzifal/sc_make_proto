# sc_make_proto_auto

Create the Star Citizen telemetry schema file straight from your installed game, with one command.

## What is this for?

Star Citizen can share live game data with other programs on your PC, such as your ship's speed, fuel, shields, landing gear and your character's health. Tools like dashboards, Stream Deck buttons and sim-racing style displays can use this data.

To read that data, a program needs a "map" of it: a list of every value the game sends and what it is called. This map is a **`.proto` file**.

The game doesn't ship this file on its own, but the information is built into the game itself. This script finds it and saves it as a `.proto` file for you.

Because it reads the game you're actually running, the file always matches your exact game version, whether that's LIVE or PTU.

## What you need

- Windows
- [Python](https://www.python.org/downloads/) 3.10 or newer
- Star Citizen installed

Then install the one extra package the script needs. Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

## How to use it

1. Run the script:
   ```
   python make_proto.py
   ```
2. It shows `Waiting for StarCitizen.exe to start...`.
3. Start Star Citizen (LIVE or PTU) and wait until you reach at least the **main menu**. If the game is already running, the script carries on straight away.
4. A moment later the script saves the file and closes:
   ```
   Waiting for StarCitizen.exe to start...
   Wrote F:\...\sc_make_proto_auto\sc_telemetry_5.0.202.10025.proto
   ```

That's it. The game doesn't need to stay open after this.

## What you get

A file named after your game version, saved **in the same folder as `make_proto.py`**:

```
sc_telemetry_5.0.202.10025.proto
```

- The number is the game's file version, the one shown in `StarCitizen.exe` > Properties > Details. It is not the version shown in the RSI Launcher.
- If a file with the same name already exists, it is replaced.
- Run the script again after each game patch to get an up-to-date file.

The file starts with a short note saying what it is and which game version it belongs to. After that come the game's data definitions:

```proto
// Compatible with: StarCitizen.exe file version 5.0.202.10025 (PTU).

message Gauge { float current = 1; float max = 2; }

message VehicleFlightInstruments {
  float speed = 1;
  float hydrogen_fuel = 2;
  float quantum_fuel = 3;
  ...
}
```

## Tested game versions

| Channel | Version in RSI Launcher | StarCitizen.exe file version |
|---|---|---|
| LIVE | 4.10.3-live.13220821 | 4.10.201.48085 |
| PTU | 5.0.0-ptu.13248297 | 5.0.202.10025 |

Other versions will most likely work too.

## How it works

1. It waits until `StarCitizen.exe` is running, checking every 2 seconds.
2. It finds where the game is installed and reads its version number.
3. It looks inside the game file for the built-in telemetry data map.
4. It turns that map back into a normal, readable `.proto` file and saves it.

It only **reads** the game file. It doesn't change the game, connect to the internet, or need admin rights. Once the game is found, it takes under half a second.

## Good to know

- **Load to the main menu first.** The script needs the game to be running.
- **One file per game version.** After a patch, run it again.
- **No comments from CIG.** The game doesn't contain the original notes that explain each value, so the file only has names and types.
- **Running more than one copy of the game?** The script uses the first one it finds.
- **Windows only.**

## For developers

The generated file is a standard proto3 file and compiles as is. For example, with `grpcio-tools`:

```
pip install grpcio-tools
python -m grpc_tools.protoc -I. --descriptor_set_out=sc_telemetry.pb sc_telemetry_5.0.202.10025.proto
```

- **Python code generation:** Python can't import module names containing dots, so rename the file first:
  ```
  copy sc_telemetry_5.0.202.10025.proto sc_telemetry.proto
  python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. sc_telemetry.proto
  ```
- **Two files merged into one:** the game stores the schema as two files, `sc/game/telemetry/v1/types.proto` and `api.proto`, in the same package. The script merges them into one file.
- **Proto3 only:** if a future game version used proto2, the script would stop with "No telemetry proto found" rather than write a wrong file.
- **Options are not written out.** The file still compiles.
- **The game's API:** the service is `GameTelemetryService`, served locally on `127.0.0.1:2942`. It has a `TelemetryStream` call for live data, and in 5.0 and later also `ListTriggerableActions` and `TriggerGameAction`.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

## Disclaimer

This is an unofficial fan project and is not affiliated with or endorsed by Cloud Imperium Games. Star Citizen® is a registered trademark of Cloud Imperium Rights LLC. The script only reads files from your own local game installation and does not connect to or modify the game.
