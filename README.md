# SRS ExternalAudio Remote + ATC Flow

Patched `DCS-SR-ExternalAudio` that can transmit to a **remote** SRS host (`--ip`), plus a Python ATC / Mission Flow planner for Stream Deck and desktop use.

## Requirements

- Windows 10/11 x64
- [Python 3](https://www.python.org/) on `PATH` (`py -3`)
- .NET desktop runtime only if you **rebuild** ExternalAudio (prebuilt exe is included)

## Quick start

1. Clone this repo anywhere.
2. Copy the example config:

   ```powershell
   Copy-Item atc\config.example.json atc\config.json
   ```

3. Edit `atc\config.json` (Opus URLs/user, airport defaults).  
   `external_audio_exe` defaults to `../DCS-SR-ExternalAudio.exe` (relative to `atc\`).
4. Launch:

   - `atc\Open-ATC-Setup.cmd` or `atc\Open-Flight-Flow.cmd`

5. Optional Google Neural2 voices: see [atc/README.md](atc/README.md). Put your service-account JSON in `atc\secrets\` (gitignored).

## Layout

| Path | Purpose |
|------|---------|
| `DCS-SR-ExternalAudio.exe` + DLLs | Slim Windows build with `--ip` / hostname support |
| `runtimes\win-x64\` | Native Speech / gRPC libs |
| `atc\` | Flow planner, phrase board, Stream Deck scripts |
| `patches\` | Source files + rebuild instructions for the `--ip` patch |

## Rebuild ExternalAudio

See [patches/README.md](patches/README.md). Upstream project: [ciribob/DCS-SimpleRadioStandalone](https://github.com/ciribob/DCS-SimpleRadioStandalone) (GPL-3.0).

## Manual transmit example

```powershell
.\DCS-SR-ExternalAudio.exe --text="Radio check." --freqs=251.0 --modulations=AM --coalition=2 --ip=your.srs.host --port=5002 --name=LocalTrigger --volume=0.5
```

## License

- `DCS-SR-ExternalAudio` binaries and `patches\` are derived from Ciribob’s SRS (GPL-3.0). See [LICENSE](LICENSE).
- The `atc\` Python tools in this repo are provided alongside that for squadron use; keep GPL obligations if you redistribute the ExternalAudio binaries.
