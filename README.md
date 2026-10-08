# QWERTY-to-MIDI

Play your PC keyboard like a MIDI instrument. QWERTY-to-MIDI turns key presses into MIDI notes on a virtual MIDI port, so you can control a DAW, synth or any other MIDI software.

The controls are modelled on the Guild Wars 2 instrument hotkeys, so they'll feel familiar if you play GW2 instruments.

## Features

- Rebindable hotkeys for all 13 notes (C to high C), octave up/down, sustain, volume and MIDI on/off
- Two sustain modes:
  - **Tap to release**: notes ring for the set note length; tap Sustain to cut them all
  - **Hold to sustain**: notes ring while Sustain is held, then fade after the note length
- Adjustable note length, velocity and transpose

## Requirements

- Windows
- A virtual MIDI port such as [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html).

## Usage

1. Start loopMIDI and create a port.
2. Run `QWERTY-to-MIDI.exe` from the Releases page, or run it from source (see below).
3. Pick your port (click ⟳ if it isn't listed), then press the **MIDI Toggle** key or click **Start MIDI**.

## Running from source

Requires Python 3.10+.

```sh
pip install -r requirements.txt
python QWERTY-to-MIDI.py
```

## Building the .exe

```sh
pip install pyinstaller
pyinstaller QWERTY-to-MIDI.spec
```

## License

[GPL-3.0](LICENSE)
