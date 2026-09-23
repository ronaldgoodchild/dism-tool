# Roadmap / ideas

Comment on (or open) an issue first so we don't duplicate work.

## Good first issues
- [ ] Add screenshots to the README
- [ ] Replace `wmic` (removed from recent Windows 11 builds) with PowerShell/CIM for the disk list
- [ ] Split the 3,500-line `dism_tool.py` into modules (dialogs / servicing / deploy / storage)
- [ ] Add a `--version` flag

## Safety
- [ ] Extra confirmation (type the disk number) before any destructive `diskpart` action
- [ ] "Dry run" that shows the exact command without executing it

## Features
- [ ] Save and replay task sequences
- [ ] Unattend.xml helper and driver-injection wizard
- [ ] Build a bootable WinPE ISO/USB from the GUI
- [ ] GitHub Actions: lint + build a signed `.exe`
