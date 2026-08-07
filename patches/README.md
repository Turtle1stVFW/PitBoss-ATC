# ExternalAudio `--ip` patch

Stock SRS ExternalAudio connects only to loopback. These two files restore a configurable SRS host so you can transmit to a remote server (e.g. `showtime.example.com:5002`).

## Files

| File | Drop into upstream tree at |
|------|----------------------------|
| `Program.cs` | `DCS-SR-ExternalAudio/Client/Program.cs` |
| `ExternalAudioClient.cs` | `DCS-SR-ExternalAudio/Client/ExternalAudioClient.cs` |

## Rebuild (Windows)

1. Install the [.NET SDK](https://dotnet.microsoft.com/download) matching the project TFM (currently `net10.0-windows…`).
2. Clone upstream:

   ```powershell
   git clone https://github.com/ciribob/DCS-SimpleRadioStandalone.git
   cd DCS-SimpleRadioStandalone
   ```

3. Overwrite the two client files with the copies from this `patches\` folder.
4. Build:

   ```powershell
   dotnet build DCS-SR-ExternalAudio\DCS-SR-ExternalAudio.csproj -c Release
   ```

5. Copy the Release output (exe + DLLs) over the binaries in this repo root. Keep `opus.dll` / `libmp3lame.dll` next to the exe. Prefer a slim Windows publish (`runtimes\win-x64` only) — do not commit iOS/Android/Linux runtime packs.

The ExternalAudio project references `Common` and `SharedAudio` in the same upstream solution; you need the full clone to build, not just these two files.

## Verify

```powershell
.\DCS-SR-ExternalAudio.exe --help
```

Confirm `--ip` is listed, then smoke-test:

```powershell
.\DCS-SR-ExternalAudio.exe --text="Radio check." --freqs=251.0 --modulations=AM --coalition=2 --ip=YOUR_SRS_HOST --port=5002 --name=PatchTest
```
