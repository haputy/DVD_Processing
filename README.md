# DVD Processing

Rips TV show DVDs, works out which title is which episode from what is said in it, and puts
the episodes on the Jellyfin NAS named the way Jellyfin reads them
(`TVShows/<Show (Year)>/Season 1/<Show> S01 E01.mp4`).

## Install

```
python -m pip install -e .[transcribe,dev]
```

The `transcribe` extra brings faster-whisper and the matching libraries. The TMDB API key goes
in `~/.dvd_processor/config.json` (the first run asks for it). If `dvd-process` is not found,
use `python -m dvd_processor` instead, or add Python's user Scripts folder to PATH.

## How to use

1. Put a disc in the drive and run

   ```
   dvd-process rip --show "NewsRadio"
   ```

   It rips every title of 10 minutes or more into `C:\Video\<disc label>`, encodes MP4s with
   HandBrake and transcribes them.
2. Repeat for each disc of the show.
3. When all the discs are done:

   ```
   dvd-process match --show "NewsRadio"
   ```

   Check the table. Rows flagged CHECK deserve a look. Type `3=S02E05` to change a row,
   `skip 3` to leave one out, then `ok` to copy to the NAS. The `C:\Video` originals stay.

Discs ripped before this tool existed can be brought in with
`dvd-process add --show "NewsRadio" C:\Video\NEWSRADIO_S3D1 C:\Video\NEWSRADIO_S3D2`.

A show already on the NAS in the wrong order can be fixed in place with
`dvd-process fix --show "Arrested Development" --nas-folder "Arrested Development (2003)"`.
If a rename on the NAS is interrupted, `dvd-process resume` finishes it.

`dvd-process scan` lists what is on the disc in the drive.

## The window

`python -m dvd_processor gui` (or `launch.bat`, or the Desktop shortcut that
`make_shortcut.ps1` makes) opens "DVD to Jellyfin":

- Type the show at the top and press Enter or Find, then check the dropdown picked the right
  one (it shows the year and TMDB id). Season is optional but makes the match surer. The
  drive with a disc in it is picked.
- **Disc to NAS**: put a disc in and press **Go**. It rips every episode-length title (play-alls
  and extras are skipped), ejects the disc, encodes, transcribes, matches this disc's titles
  against the episodes not yet on the NAS, and copies them, with no questions. It copies
  nothing and stops for review when a match is flagged CHECK, two titles get one episode, or
  there are more titles than episodes; the table opens on Match and copy. Press Go again
  with the same disc to carry on after a stop or an error: a finished rip is not redone, an
  unfinished one is ripped again into a new folder, and nothing on the NAS is overwritten.
- **Movies**: switch the top bar from TV to Movie, find the film (the year tells remakes apart)
  and press Go. It rips the main feature only, encodes it and copies it to
  `//DXP2800-4CC5/Jellyfin/Movies/Title (Year)/Title (Year).mp4` (config `nas_movie_root`). A
  film whose folder already holds a video is never copied again. A disc with two titles about
  as long as the film (two cuts, or decoy titles) rips nothing and asks you to click the one to
  rip in the table, then Go again.
- **Match and copy**: where Go's stops are reviewed; Match also matches every disc of the show
  at once. Match fills the table. Yellow rows are CHECK and start unticked; click
  an episode to change it (the row becomes FIXED and ticked). Copy to NAS stays disabled
  while two ticked rows share an episode.
- **Fix a show on the NAS**: pick the show's folder, Match, then Rename the ticked rows. An
  unfinished rename shows a banner with Resume.

The log at the bottom shows progress; full detail goes to `~/.dvd_processor/gui.log`.

See `docs/plans/2026-10-17-transcript-matcher.md` for how the matching works.
