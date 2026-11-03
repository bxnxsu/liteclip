# LiteClip

A tiny instant-replay clipper for Windows, like Medal or ShadowPlay, but without the bloat.

LiteClip keeps the last few seconds of your screen in a rolling buffer. Press a hotkey and those seconds are saved as an MP4. Both screen capture (Desktop Duplication) and encoding (NVENC) run on your NVIDIA GPU, so it barely touches your CPU and your games stay smooth.

## Features

- Instant replay: save the last 15 to 120 seconds with one hotkey
- GPU capture and encoding, with almost no CPU or memory use
- A global hotkey that works in fullscreen games, and warns you if another app already uses it
- Runs quietly in the system tray, and can start with Windows
- A recent clips list with thumbnails and Play, Show and Delete buttons
- H.264 (plays everywhere, including Discord) or HEVC (smaller files)
- Optional desktop audio
- No accounts, no uploading, no overlay, no ads

## Requirements

- Windows 10 or 11
- An NVIDIA GPU (GTX 900 series or newer) with up-to-date drivers
- [FFmpeg](https://ffmpeg.org) 6.1 or newer. If it's missing, LiteClip offers to install it for you on first launch, using `winget install Gyan.FFmpeg`

## Getting started

### Option 1: Use the installer (recommended)
1. Download `LiteClip-Setup-x.x.x.exe` from the [Releases](../../releases) page.
2. Run it and follow the wizard. No admin rights are needed. If FFmpeg isn't installed yet, the wizard installs it for you.
3. Open LiteClip from the Start menu.

Windows may show *"Windows protected your PC"*, because the installer isn't code-signed. Click **More info → Run anyway**.

To uninstall, go to **Settings → Apps → Installed apps → LiteClip → Uninstall**. Your clips are never deleted.

### Option 2: Run from source (Python 3.10+)
```
pip install -r requirements.txt
pythonw liteclip.pyw
```

### Option 3: Build it yourself
Double-click `build_exe.bat`. It builds the app into `dist\LiteClip\`, then makes the setup wizard `dist\LiteClip-Setup-x.x.x.exe` using [Inno Setup](https://jrsoftware.org/isinfo.php), and offers to install Inno Setup if you don't have it.

To release a new version, change `APP_VERSION` at the top of `liteclip.pyw`, then run `build_exe.bat` again.

## How to use

1. Open LiteClip and click **Start recording**.
2. Play your game. When something cool happens, press your hotkey (**Alt + F8** by default).
3. You'll hear a beep, and the clip is saved to `Videos\LiteClips`.

Change the clip length, frame rate, quality, hotkey and save folder in **Settings**.

## Desktop audio

FFmpeg needs an audio device to record from. Either enable **Stereo Mix** (Sound settings → Recording devices), or install a virtual audio cable such as VB-Audio Cable. Then pick it under **Settings → Audio**.

## Troubleshooting

- **Black or empty clips:** switch your game to borderless windowed mode.
- **The hotkey doesn't work:** another app may already be using it. NVIDIA's overlay uses Alt+F10, for example. Pick a different one in Settings.
- **Clips won't save to a folder:** Windows Security's *Controlled folder access* may be blocking it. Allow LiteClip and ffmpeg.exe, or choose another folder.
- **Recording stops by itself:** LiteClip restarts the recorder automatically after hiccups like UAC prompts or a game changing resolution. If it keeps stopping, it shows you the reason.
- **Anything else:** Settings → Troubleshooting → **View log** shows FFmpeg's last error.

## How it works

FFmpeg records into short 2-second chunks that are constantly overwritten. When you press the hotkey, LiteClip joins the most recent chunks into an MP4 without re-encoding them, so saving is almost instant.

The recorder is tied to LiteClip through a Windows job object, so it can never be left running in the background, even if LiteClip crashes or is ended in Task Manager. Deleted clips go to the Recycle Bin.

## Licence

MIT. See [LICENSE](LICENSE). FFmpeg is a separate project with its own licence (LGPL/GPL) and is not included in this repository.
