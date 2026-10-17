# Transcript matcher: rip, encode, transcribe, match, copy

Agreed with Andy on 17 October 2026, after the approach worked on all of NewsRadio (97
episodes over 8 discs) and fixed Arrested Development's order on the NAS. The prototype is
`C:\Users\andy\Dropbox\Projects\episode-matcher` (transcribe.py, wiki_episodes.py,
tmdb_numbers.py, match.py, plan.py, apply.py). This plan brings that into the package and
replaces the duration matcher.

## Why

TMDB and TheTVDB store whole-minute, often nominal runtimes, so matching by duration cannot
tell episodes apart. Dialogue can. Andy also wants to put a disc in, start it, and have the
rest done.

## How Andy actually works (read off his files and HandBrake settings, 17 October 2026)

- MakeMKV rips every title to MKV in `C:\Video\<DISC LABEL>\`.
- The HandBrake GUI (1.9.2) encodes with its default preset, **Fast 1080p30** (H.264, AAC
  stereo 160 kbps, MP4, DVD resolution kept), into an `MP4\` subfolder, named with
  underscores removed and title case: `A1_t00.mkv` becomes `MP4\A1 T00.mp4`.
- The NAS is `\\DXP2800-4CC5\Jellyfin`. TV naming already in use:
  `TVShows\<Show (Year)>\Season <n>\<Show> S01 E01.mp4` (note the space before E).
- HandBrakeCLI 1.9.2 is at `%LOCALAPPDATA%\HandBrakeCLI\1.9.2\HandBrakeCLI.exe` (checksum
  verified). makemkvcon64 is at `C:\Program Files (x86)\MakeMKV\makemkvcon64.exe`.
- The TMDB key is in `~/.dvd_processor/config.json`.

## The flow

1. **Rip** (`dvd-process rip --show "NewsRadio" `): makemkvcon rips every title of 10 minutes
   or more into `C:\Video\<label>\`. No matching up front. The disc folder gets a small
   `disc.json` recording the show, so later steps know which show it belongs to.
2. **Encode**: HandBrakeCLI, preset "Fast 1080p30", into `<disc>\MP4\<Title Case>.mp4`.
3. **Transcribe**: faster-whisper `small.en` on the GPU (float16), falling back to CPU int8.
   Cached as `<disc>\transcripts\<title>.txt`. About 20 seconds per episode on the RTX 4070.
4. **Match** (`dvd-process match --show "NewsRadio"`): every transcribed title of that show,
   across all its disc folders, against every episode at once.
   - Reference text per episode: TMDB name and overview, plus the Wikipedia summary when the
     show has a "List of ... episodes" page or season pages.
   - TF-IDF with sublinear tf, English stop words plus the regulars' names (TMDB's main cast
     character names), max_df 0.5.
   - Titles under 10 minutes are extras and skipped.
   - A gentle disc-order prior: bonus 0.05 * exp(-|title position - episode position| / 1.5),
     positions scaled across the show. Strong dialogue still overrules it.
   - One assignment for the whole show (scipy linear_sum_assignment), so each episode is used
     once and an episode the DVD files elsewhere still lands right.
   - Numbering is TMDB's, because Jellyfin labels by TMDB.
   - Each row gets a score and a margin over the runner-up; rank 1 with margin over 0.02 is
     confident, anything else is flagged CHECK.
5. **Review**: the existing rich confirmation table, now with score, margin and CHECK, and the
   existing `1=3` corrections.
6. **Copy** to the NAS: copy to `<dest>.part`, check the size, rename into place, never
   overwrite. The `C:\Video` originals stay.
   A **rename** mode exists for shows already on the NAS: two passes through `.renaming`
   names, files already right are skipped, and an interrupted run can be resumed.

## Lessons from the prototype to carry over

- Use forward-slash UNC paths (`//DXP2800-4CC5/Jellyfin/...`) internally, and compare paths
  with slashes normalised. A lost backslash stranded 40 files mid-rename on 17 October.
- PyAV 16 breaks faster-whisper 1.2.1: pin `av<16`.
- On Windows, add the `nvidia/*/bin` folders from site-packages to the DLL search path before
  importing faster_whisper, or CUDA fails to find cublas64_12.dll.
- The CPU fallback catches CUDA failures that raise a Python exception (a missing DLL, no
  driver, out of memory at load or on the warm-up transcription). A hard CUDA crash that
  kills the process (a native access violation in the driver or CTranslate2) is not caught:
  the run stops and has to be restarted, with the GPU disabled if it keeps happening.
  Cached transcripts mean a restart only redoes the title that was in progress.
- Microsoft Store Python redirects venvs under `%LOCALAPPDATA%\Packages\...`; create the venv
  with a non-Store Python if available, or use the redirected path.

## Out of scope for now

The GUI keeps working for scanning, but the new flow is CLI first; the GUI catches up after.
OpenSubtitles is a possible later reference source for shows with thin summaries.
