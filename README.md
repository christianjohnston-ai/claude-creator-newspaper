# Creator Newspaper, a free Claude skill

**Install it by pasting this into Claude Code:**

```
Install the creator-newspaper skill from https://github.com/christianjohnston-ai/claude-creator-newspaper into ~/.claude/skills/creator-newspaper, read its SKILL.md, and set me up.
```

An AI that watches the reels in your niche every night, transcribes them, looks at the frames,
measures how each one did, and writes it all into one newspaper you read in the morning. You get
the top content in your niche without being consumed by the doom scroll.

Built from the newspaper I read every day, rebuilt to run on your own computer with tools anyone
can sign up for.

## What it does

Every night, for the Instagram and TikTok creators you pick:

1. **Pulls** their new public reels and the numbers (views, likes, comments) through
   [Apify](https://apify.com). It shows the cost before every run and stops at the monthly limit you set.
2. **Watches** each reel on your computer: downloads it, transcribes it, pulls frames (closely in the
   first 3 seconds, where the hook is, then about every 2 seconds and at every scene change) and
   makes a contact sheet that Claude looks at to see the animations, the screens and the on-screen text.
3. **Measures** each reel once, at 3 days old, against that creator's own median views.
Then, when you say "make my newspaper" (in the morning, or any time), Claude reads the transcripts and
the contact sheets and **writes the paper**. The nightly run (optional, on a Mac) does steps 1 to 3 so
the paper is quick to write when you ask.

Each paper is one HTML file that works offline and on a phone:

- **The lead**: a headline and a short story on the most important thing in your niche since the last paper
- **Breaking out**: every reel that hit 3x its creator's usual views, with frames, the hook word for
  word, and why it worked
- **Themes**: ideas two or more creators landed on, and how their angles differ
- **Every new reel**, creator by creator: a frame, the numbers, its label (or "too young" with its
  views so far), a link, and the transcript in a tap-to-open box
- **Since the last paper**: earlier reels that got their 3-day label since

Nothing repeats between papers unless something new happened to it.

## How a reel is measured

- A reel is judged once, at its first reading at 3 days old or older.
- It is compared with the median views of the same creator's other judged reels. The first run reads
  each creator's last 30 reels as the baseline. Those are usually older than 3 days when read, so they
  carry more views: the comparison is strict, never generous.
- **Breakout**: 3x the median or more. **Above usual**: 1.5x to 3x. **Usual**: 0.5x to 1.5x.
  **Below**: under 0.5x. With fewer than 10 reels to compare with: "not enough history yet".
- Younger reels show "too young: X views in Y hours".
- After about two weeks of saved readings, a young reel with 3x the creator's usual views at the
  same age is flagged "taking off early".

Every label in the paper shows the median it was compared with and how many reels that came from.
The paper is refused if any number or quote in it does not match the saved data.

## Costs (Apify prices as of November 10, 2026)

- Apify's **free plan includes $5 of usage a month, with no card**. Unused credit does not roll over.
- **Instagram**: Apify's Instagram Reel Scraper (`apify/instagram-reel-scraper`) costs about **$2.60
  per 1,000 reels** on the free plan, so the free credit covers about **1,900 readings a month**
  (roughly 30 new reels a night, see below). More needs a paid Apify plan.
- **TikTok**: the TikTok Scraper (`clockworks/tiktok-scraper`, the most used TikTok scraper in the
  Apify Store) costs $3.70 per 1,000 results on the free plan. A nightly pull of new TikToks also uses
  its date filter and video download add-ons ($1.30 per 1,000 each), so about $6.30 per 1,000.
- Every actor run also costs $0.001 to start.
- Each new reel is read twice (when it is new, and again at 3 days to measure it), and each new
  creator's last 30 reels are read once. So 1,900 readings a month is roughly 30 new reels a night.
  Run `python3 scripts/fetch.py --budget` to see the math for your own limit.
- Transcribing and frames run on your own computer and cost nothing.

Prices change. The skill tells Claude to check the actors' pages before setting your budget, and
every run shows its estimate first.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- A Mac (made and tested on macOS) or Linux
- Python 3.9 or newer. The `python3` that comes with macOS works.
- [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg` on a Mac)
- A speech-to-text tool that runs on your computer, installed into the skill folder's own `.venv`:
  `mlx-whisper` on Apple Silicon Macs, `faster-whisper` elsewhere. The first run downloads the speech
  model (about 1.6 GB for mlx-whisper's default, about 500 MB for faster-whisper's default).
- A free Apify account

Claude walks you through all of it on the first run.

## Manual install

```
git clone https://github.com/christianjohnston-ai/claude-creator-newspaper ~/.claude/skills/creator-newspaper
```

Then open Claude Code and say "set up my newspaper". After that, any morning: "make my newspaper".
To install it for one project only, clone it into that project's `.claude/skills/creator-newspaper`.

## Your token and your data

You save your Apify token in a text file; Claude moves it into the skill folder's `.env` (readable
only by you), checks the copy without showing it, and deletes the original. It is never printed,
never put in a URL, and only ever sent to api.apify.com. `.env`, your config and everything the skill
saves (`data/`, `runs/`, `editions/`, videos) are in `.gitignore` and stay on your computer.

## Play fair

Only pull public posts, follow each platform's terms, and keep the paper for your own reading. Do not
repost or share other creators' videos.

## Files

| File | What it does |
|---|---|
| `SKILL.md` | the instructions Claude follows: setup, the nightly run, the writing rules, fixes |
| `scripts/fetch.py` | pulls reels and numbers from Apify, estimates and caps the cost |
| `scripts/watch.py` | downloads, transcribes, pulls frames, makes contact sheets |
| `scripts/measure.py` | labels each reel against the creator's median |
| `scripts/brief.py` | gives Claude what it needs to write, and a template |
| `scripts/render.py` | checks Claude's writing against the data and prints the paper |
| `scripts/check_token.py` | moves and checks your Apify token without showing it |
| `scripts/schedule.py` | optional nightly run on a Mac (and how to turn it off) |
| `config.example.json` | the settings, copied to `config.json` on setup |

Made by Christian Johnston (@christianjohnston.ai on Instagram)

MIT License
