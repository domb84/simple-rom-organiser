# Saves: what is found, how it is tied to a game, and what a library build does with it

*v0.2, this branch. A system with no save source set up (RetroArch for most, an emulator on the Emulators page for GameCube, Wii, Wii U,
PS2 and Switch) shows no saves anywhere.*

| | RetroArch | Dolphin, GameCube | Dolphin, Wii | Cemu, Wii U | PCSX2, PS2 (folder card, states) | PCSX2, PS2 (card image) | Eden, Switch | Ryujinx, Switch |
|---|---|---|---|---|---|---|---|---|
| **Systems** | all except the five on the right | GameCube | Wii | Wii U | PlayStation 2 | PlayStation 2 | Switch | Switch |
| **Detected?** | yes: flatpak, Steam, native, Windows installs, from `retroarch.cfg`; or choose one | yes: flatpak, native, Windows; a portable Dolphin: choose the folder | same | yes: flatpak, native, Windows (reads `mlc_path` too) | yes: flatpak, `~/.config`, Documents, Windows (reads `PCSX2.ini`, flatpak portal paths too); portable: choose | same | yes: `~/.config` / `~/.local/share` of Eden, yuzu, Sudachi, Suyu; the flatpaks (`dev.eden_emu.eden`, `org.yuzu_emu.yuzu`: ids from the projects' names, not tried here); Windows `%APPDATA%\eden` | yes: standard place (`~/.config/Ryujinx`, Windows `%APPDATA%\Ryujinx`), or a portable one found next to the games |
| **Tied to a title?** | yes: the save's file name = the ROM's name (also a zip's member, and a multi-disc game's `.m3u` name) | yes: game code in the `.gci` header | yes: game code in the folder name | yes: title ID, via Cemu's own game list (a file Cemu has not listed has no saves here) | yes: serial in the folder name / state name; the disc's serial read from `SYSTEM.CNF` | yes: serial of each save in the card | yes: title ID of the base game (updates and add-ons share it) | yes: title ID in `ExtraData0` |
| **Tied to a release?** | **yes**: the name is the exact release (`Game (USA)` ≠ `Game (Europe)`); a save of another release of the title is counted under the title ("no ROM here") but does not protect this ROM | region yes (the 4th character of the code); revision and disc: no | same | region yes (title IDs differ per region); revision: no | region yes (serial); revision: no | same | no: one title ID for every region / version of a game, updates and add-ons included | same |
| **Renamed with the ROM?** | **yes** (a rename of the ROM renames its saves; copied when the build is into another folder; switch on the RetroArch settings) | no, never: the ID does not change | no | no | no | no | no | no |
| **Library build** | renames the saves, keeps a game that has saves | counts, keeps a game that has saves; nothing else is touched | same | same | same | same | same | same |
| **Title archived (rules replace it)** | your choice, per game or for all: **keep both ROMs** (default: the game stays), **archive the saves with the ROM** (to `<archive>/<system>/_saves`), **leave** the saves | same three choices; archive moves the `.gci` files | same; moves the game's `data` folder | same; moves the account folders (`80000001`, `common`) | same; moves the save folders and the save states | **counted, never moved** (many games share one image) | same; moves Eden's save folder | **counted, never moved** (a counter folder plus `ExtraData0` must stay together) |
| **Undo** | the build's undo puts moved / renamed saves back | yes (journal linked to the build) | yes | yes | yes | n/a | yes | n/a |
| **Emulator running?** | checked: nothing moves while it runs (it writes saves back on exit); the Library preview says so | checked | checked | checked | checked | n/a | checked (Eden, yuzu, Sudachi, Suyu) | n/a |

## Also relevant

* **What is counted.** RetroArch: save files (`.srm`, `.eep` ...) and save states (`.state`, `.state1` ...) per ROM; state screenshots are
  listed but not counted. Dolphin GC: one per `.gci`; GC memory-card images (`.raw`) as one entry each. Wii: one per game. Wii U: one per
  account folder (and the common one). PCSX2: one per save folder in a card, plus the save states (`SERIAL (CRC).01.p2s`; `.backup` copies
  are ignored). Switch: one per profile / device save, per emulator.
* **Where it shows.** Overview ("Saves (Dolphin)" card), Browse (a Saves column, a "games with saves" filter and a Saves list that says
  which saves have no ROM here), the Library preview (rows say *kept*, *archive* or *leave*), the systems list (a save count), and
  Collection (per system, with the same choices).
* **Two copies of one game.** An ID-based save belongs to the *first* file of that ID in the list; the second copy has none and the rules
  may archive it. RetroArch saves belong to the file with that exact name.
* **Choices.** Per game on the Library tab, or one default for the system (Library rules) or for Collection.
* **Running programs.** Before saves are archived the app looks for the program's process (Linux `/proc`, Windows process list). Saves of a
  running program stay where they are; the others in the same build still move, and the preview and the result say which program it was.
* **Collection and emulator folders.** An emulator's data folder that lies inside the Collection's ROM folder (a portable Ryujinx with the
  games in it, say) is left alone: its saves, caches and settings are never sorted or archived as "files that are not ROMs". Only the
  Switch game files inside a Ryujinx folder are looked at.
* **Not built.** Copying saves between emulators (Eden to Ryujinx, per `docs/SWITCH_PLAN.md`), saves of other standalone emulators
  (DuckStation, Flycast ...).
