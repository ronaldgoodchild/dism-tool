# DISM Tool

A free **Windows image servicing and deployment GUI** built on `dism.exe`, `diskpart` and `robocopy`. Manage WIM/ESD/VHD images, packages, features, drivers, AppX packages and Windows PE - plus disk tools and log diagnostics - without memorising DISM switches. Pure Python + Tkinter, no dependencies.

> Built by a working IT technician who builds and services Windows images.

## Features

- **Image Servicing** - mount / unmount / commit images, image info with a smart index picker, cleanup, health checks
- **Packages, Features, AppX, Capabilities, Drivers** - list, add, remove (Feature Manager dialog with filter)
- **Windows PE** - PE image tasks
- **Deploy** - capture and apply images, export and convert (WIM / ESD), robocopy-based file copy
- **Storage** - physical disk listing and `diskpart` scripting with a disk picker
- **Diagnostics** - DISM log viewer and health tools, live-streaming output window, recent-files memory

## Requirements

- Windows 10 / 11 (or WinPE with Python)
- Python 3.8+ (standard library only)
- **Run as Administrator**

## Quick start

```powershell
git clone https://github.com/ronaldgoodchild/dism-tool.git
cd dism-tool
python dism_tool.py
```

## Safety - please read

The **Storage / disk tools can erase disks**, and DISM operations can modify or corrupt images. Double-check every target, work on copies of your images, and never run against a disk you have not backed up. Use it only on systems you own or are authorised to service. No warranty - see [LICENSE](LICENSE).

## Contributing

Ideas and pull requests welcome - see [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

## License

[MIT](LICENSE) (c) 2026 Ronald Goodchild / REGTeches
