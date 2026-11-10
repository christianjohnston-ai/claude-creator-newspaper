---
name: creator-newspaper
description: Use when the person says "make my newspaper", "morning paper", "my paper", "what did my creators post", "set up my newspaper", "add a creator to my newspaper", or asks for a digest of the new reels from the Instagram or TikTok creators they follow. Pulls each creator's new public reels through Apify (cost shown and capped by a monthly limit before every run), downloads and transcribes each video on this computer, pulls frames and a contact sheet that Claude looks at, measures each reel at 3 days against that creator's median views, and prints one offline HTML newspaper per edition with nothing repeated between editions. First run interviews the person and sets everything up.
---

# Creator Newspaper

Every night: pull the new reels from the creators the person follows, watch them (transcript,
frames, contact sheet), and measure how each one did against that creator's usual numbers. In
the morning: Claude reads it all and writes one newspaper the person reads instead of scrolling.

The scripts do the counting and the files. Claude does the reading and the writing. Never type a
number into the paper by hand: every number comes from the saved data, and `render.py` refuses to
print a paper whose numbers or quotes do not match it.

## Where everything lives

All paths are relative to this skill folder (the folder holding this SKILL.md), called HOME below.
Run every script from HOME: `cd <HOME> && python3 scripts/<name>.py`.

| Path | What it is |
|---|---|
| `config.json` | the person's niche, creators, budget and prices (from `config.example.json`) |
| `.env` | `APIFY_TOKEN=...`, chmod 600. Never print it, never `cat` it, never commit it |
| `data/` | `reels.json`, `readings.json` (every reading with its time), `labels.json` (frozen labels), `history.json`, `spend.json`, `state.json` |
| `runs/<date>/` | one fetch: `raw/` actor output, `videos/`, `watch/<reel>/` (transcript, frames, contact sheet), `measure.json`, `editorial.json` |
| `editions/<date>.html` | the printed papers |

## First run: set up (when there is no config.json or no .env)

Go one step at a time. The person may not be technical: say exactly what to click or paste, and
wait for them to say it worked before the next step. Plain words, no long dashes.

### 1. Play fair (say this, ask them to confirm they read it)
- This reads PUBLIC posts only, through Apify (apify.com), a service that collects public web data.
  Follow each platform's terms. The paper is for their own reading: do not repost or share other
  creators' videos.
- Apify costs money after its free monthly credit. Before every pull the skill estimates the cost
  and stops if it would pass the monthly limit they choose. Apify also stops each run at that limit.
- Their API token stays in one private file on their computer. Claude never prints it, never puts it
  in a URL and never writes it anywhere else.

### 2. Interview (one question at a time, then a short summary to confirm)
1. Their niche, and what they want to learn from these videos (hooks, formats, topics, editing).
2. Platforms: Instagram, TikTok, or both.
3. Monthly Apify limit. First check today's prices on the actors' own pages,
   https://apify.com/apify/instagram-reel-scraper and https://apify.com/clockworks/tiktok-scraper
   (Pricing). If a price changed, update `prices_usd` and `checked_on` in config.json. Then write the
   limit into config.json and run `python3 scripts/fetch.py --budget` and show them the math: how many
   reel readings that buys a month, and about how many creators it covers. Each reel is read twice
   (when new, then at 3 days), and a new creator's last 30 reels are read once as the baseline.
4. Which creators, within that number (handles, or profile links; `@` is fine).
5. How far back the first paper goes (suggest 3 days): `first_window_days`.
6. Confirm where the skill lives: `~/.claude/skills/creator-newspaper` works in every project;
   `.claude/skills/creator-newspaper` inside one project works only there. Their data stays beside it.
Copy `config.example.json` to `config.json`, fill it in, and show it to them. Test: they say it is right.

### 3. The Apify token
1. They make a free account at apify.com (the free plan needs no card), open Apify Console >
   Settings > API & Integrations, copy the token, paste it into a new plain text file, and save it.
   Tell them NOT to paste the token into the chat. Ask only for the file's path.
2. Tell them the original file will be deleted after the copy is checked, then run:
   `python3 scripts/check_token.py import "<path>" --delete-original`
   It writes `.env` (chmod 600), compares the copy by SHA-256 without showing it, and deletes the original.
3. `python3 scripts/check_token.py verify` prints only the Apify username, plan and this month's usage.
If the person pastes a token into the chat anyway: do not repeat it, tell them to make a new token in
Apify Console (and delete the pasted one there), and start this step again.

### 4. Tools (check first, explain each install before running it)
1. `python3 --version`: 3.9 or newer (the one that comes with macOS is fine).
2. `ffmpeg -version`. Missing on a Mac: `brew install ffmpeg` (Homebrew from brew.sh if needed).
3. Speech to text, in a private Python environment inside HOME so nothing else on the computer changes:
   - Apple Silicon Mac (`uname -m` says arm64): `python3 -m venv .venv && .venv/bin/pip install mlx-whisper`
   - anything else: `python3 -m venv .venv && .venv/bin/pip install faster-whisper`
   The first transcription downloads the speech model (about 1.6 GB for mlx-whisper's default
   whisper-large-v3-turbo; faster-whisper's default "small" is about 500 MB). Say so before it starts.
4. `python3 scripts/watch.py --check` must name the engine.
5. Test clip (Mac): `say -o /tmp/np_test.aiff "This is a test of my morning newspaper"` then
   `ffmpeg -y -i /tmp/np_test.aiff -ar 16000 -ac 1 /tmp/np_test.wav` then
   `.venv/bin/python scripts/transcribe_one.py /tmp/np_test.wav /tmp/np_test.json` and show the text.
   Elsewhere, skip the clip; the first real reel is the test.

### 5. First pull
`python3 scripts/fetch.py --dry-run` shows every request and the estimate. Show the estimate, get
their OK, then make the first paper with the steps below.

## Make the newspaper ("make my newspaper", "morning paper")

1. **Fetch.** `python3 scripts/fetch.py --if-older-than 6`. If the night shift already ran, this skips
   (no charge). Otherwise it plans the calls, prints the estimate, refuses if the month's limit would
   be passed (exit code 3: tell them the numbers and the three ways forward), runs, and prints a table
   and the real cost. Problems print as `PROBLEM:` lines; tell the person about each in plain words.
2. **Watch.** `python3 scripts/watch.py` downloads each new video right away (scraper links expire),
   transcribes it, pulls frames (every 0.5 s in the first 3 s, then about every 2 s, plus scene
   changes) and makes one contact sheet per reel.
3. **Measure.** `python3 scripts/measure.py` labels every reel that has its 3-day reading and prints
   each label with the median it was compared with and how many reels that came from.
4. **Brief.** `python3 scripts/brief.py` lists every reel in the coming edition with its numbers, label,
   transcript start and the paths of its contact sheet, tile map and hook frame, and writes
   `runs/<run>/editorial_template.json`.
5. **Look and read.** Open every contact sheet with the Read tool (it is an image) and read its tile
   map (`contact_sheet.txt`: which tile is which second). For each Breakout also open the hook frame
   at full size. Read the transcripts (`brief.py --full` prints them whole).
6. **Write** `runs/<run>/editorial.json` from the template. Rules:
   - `headline` and `lead_story`: the most important thing that happened in their niche since the
     last paper, and why it matters for what they want to learn. Blank lines split paragraphs.
     `lead_reel_keys` lists the reels the story is about.
   - `breakouts`: one per Breakout key. `hook_quote` is the first spoken words, copied word for word
     from the transcript (the template pre-fills its first 14 words; trim, never reword).
     `why_it_worked`: 1 to 3 sentences on the mechanism you can see or hear (the opening picture, the
     claim in the first second, the pattern break, the payoff), never "great content" or "high energy".
   - `themes`: ideas two or more creators landed on. Each has `name`, `summary`, `reel_keys` (from two
     or more creators) and `angles` (one line per reel on how that creator's angle differs). Set
     `"returning": true` only when an earlier edition had a theme with the same name. With one
     creator, or no shared idea, leave `themes` empty: the paper says so.
   - `reel_notes`: for every watched new reel, at least `hook` (first words and first picture) and
     `format` (talking head, screen recording, green screen, skit, slides, b-roll voiceover). Add
     `on_screen_text`, `visuals` (animations, screens, apps shown) and `cta` when present. Only write
     what is in the frames or the transcript.
   - `since_last`: optional lines about earlier stories (a theme that came back). Label arrivals for
     earlier reels are printed by the script itself.
   - Numbers: copy them from the brief. Anything you cannot trace to a reading, label, transcript or
     caption will be refused. No long dashes anywhere (use commas, periods or hyphens).
7. **Check, then print.** `python3 scripts/render.py --check` lists every problem; fix them in
   editorial.json and check again. Then `python3 scripts/render.py` prints `editions/<date>.html`.
8. **Tell them** the file path first, then 2 or 3 lines: the headline, how many new reels, any
   Breakouts, and the Apify cost of the runs in this edition. On a Mac, `open "<path>"` opens it.

The first time, ask what they would change about the paper and adjust the writing until they like it.

## Add or remove a creator
Edit `creators` in config.json. A new creator gets its 30-reel baseline on the next fetch (the
estimate shows the cost first). Removing a creator keeps its saved data; it just stops being pulled.

## When something breaks
| What they see | What it means | What to do |
|---|---|---|
| `Apify did not accept your token` (HTTP 401) | token mistyped, revoked or expired | new token in Apify Console, then setup step 3 again |
| `Apify got a request with no token` (HTTP 401/402) | `.env` missing or has no APIFY_TOKEN | setup step 3 |
| `cannot pay for this run` (HTTP 402) | the Apify account is out of monthly credit | wait for the new month, or a paid Apify plan |
| `NOT RUNNING: this run would cost about $X` (exit 3) | the monthly limit in config.json would be passed | raise `budget.monthly_limit_usd`, remove creators, or lower `max_new_per_creator_per_run` |
| `PROBLEM: ... no reels came back` | account private, renamed, no new reels, or the scraper was blocked | check the handle on the platform; try again next night |
| reels saved but numbers or captions empty | the actor changed its output fields | open `runs/<run>/raw/*.json`, compare with the actor's page on apify.com, update `norm_instagram` / `norm_tiktok` in `scripts/fetch.py` |
| `could not download the video` | the video link expired or was removed | the 3-day re-read brings a fresh link; `watch.py` tries again then |
| `no speech-to-text tool installed` | `.venv` missing or the package not installed | setup step 4.3 |
| `NOT PRINTED. editorial.json has N problem(s)` | a number, quote or entry does not match the data | fix each listed line, run `render.py --check` again |

## Run it every night (only if the person asks)
On a Mac: `python3 scripts/schedule.py install --at 04:30` sets up a launchd job that runs fetch,
watch and measure each night (log in `logs/nightly.log`). In the morning "make my newspaper" finds
the work done and only writes and prints. The Mac has to be on; if it is asleep at that time, macOS
runs the job when it wakes. Check it: `python3 scripts/schedule.py status`. Turn it off:
`python3 scripts/schedule.py remove`. Show them the off command when you set it up.

## Never
- Print, echo, `cat` or log the token, or put it in a URL. Scripts send it only as an
  `Authorization: Bearer` header, and only to api.apify.com.
- Invent a number, a quote, a transcript or a reel. If a step failed, the paper says so.
- Run a paid pull without the estimate on screen, or work around the monthly limit.
