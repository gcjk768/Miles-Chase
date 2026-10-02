"""Obsidian vault: the app's movement log and memory (NAS vault standard).

Layout under $VAULT_DIR (on the NAS: /volume1/James/Obsidian/Miles Chase, mounted at /vault):
    Home.md                 map of contents, rewritten after each event
    Activity/YYYY/MM/YYYY-MM-DD.md  one line per event: - HH:MM emoji **what** · detail · [[entity]]
                            (SGT). Flat Activity/YYYY-MM-DD.md notes from older versions are
                            moved into YYYY/MM/ by migrate(), called once at startup.
    Deals/<post title>.md   one note per deal or promo alerted, append-only ## History
    Cards/<card name>.md    one note per card or programme: balance, expiry, ## History

Writes go in after each report, answer, alert, reminder, points update or self-repair; memory()
is read back into the Claude prompts so it doesn't repeat itself, and alerted_links() stops the
news watcher re-alerting a post. Best-effort: with VAULT_DIR unset nothing happens, and an error
is printed and swallowed, never raised. Never pass secrets or whole prompts in here.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))
MEMORY_CHARS = 4000
MEMORY_DAYS = 7          # Activity notes read back as memory
ALERTED_DAYS = 120       # Activity notes scanned for links already alerted
DAY_NOTE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL = re.compile(r"https?://[^\s)\]>]+")
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]+')


def root():
    path = os.environ.get("VAULT_DIR", "").strip()
    return Path(path) if path else None


def _warn(error):
    print(f"warning: vault: {error}", file=sys.stderr)


def note_name(title):
    """A title as a safe file/wikilink name."""
    return UNSAFE.sub(" ", str(title)).strip(" .")[:80].strip() or "untitled"


def _one_line(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _write(path, text):
    """Atomic write (tmp + rename), group-writable so James can edit it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    _chmod(path)


def _chmod(path):
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass


def log(emoji, what, detail="", entity=None, now=None):
    """Append one event line to today's Activity note. Never raises."""
    base = root()
    if not base:
        return
    try:
        now = now or datetime.now(SGT)
        day = day_note(base, now)
        parts = [f"- {now:%H:%M} {emoji} **{_one_line(what, 80)}**"]
        if detail:
            parts.append(_one_line(detail))
        if entity:
            parts.append(f"[[{entity}]]")
        new = not day.exists()
        day.parent.mkdir(parents=True, exist_ok=True)
        with day.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"---\ntags: [active]\nupdated: {now:%Y-%m-%d}\n---\n"
                        f"# Activity {now:%Y-%m-%d}\n\nBack to [[Home]]\n\n")
            f.write(" · ".join(parts) + "\n")
        if new:
            _chmod(day)
        _home(base, now)
    except Exception as error:  # best-effort: never lose an alert over the vault
        _warn(error)


def day_note(base, when):
    return base / "Activity" / f"{when:%Y}" / f"{when:%m}" / f"{when:%Y-%m-%d}.md"


def day_notes(base):
    """All Activity day notes, newest first."""
    folder = base / "Activity"
    notes = [p for p in folder.rglob("*.md") if DAY_NOTE.match(p.stem)] if folder.is_dir() else []
    return sorted(notes, key=lambda p: p.stem, reverse=True)


def migrate():
    """Move flat Activity/YYYY-MM-DD.md notes into Activity/YYYY/MM/ (move, never delete).

    Returns how many were moved. Never raises."""
    base = root()
    if not base:
        return 0
    moved = 0
    try:
        for old in sorted((base / "Activity").glob("*.md")):
            if not DAY_NOTE.match(old.stem):
                continue
            new = day_note(base, datetime.strptime(old.stem, "%Y-%m-%d"))
            if new.exists():  # both exist: keep both rather than overwrite either
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
            moved += 1
        if moved:
            _home(base, datetime.now(SGT))
    except Exception as error:
        _warn(error)
    return moved


def entity(folder, title, summary, history, now=None):
    """Create or update an entity note: summary on top, `history` appended to ## History.

    Returns the wikilink target ("Deals/Title"), or None when the vault is off or failed."""
    base = root()
    if not base:
        return None
    try:
        now = now or datetime.now(SGT)
        name = note_name(title)
        path = base / folder / f"{name}.md"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = old.split("\n## History\n", 1)[1].strip().splitlines() if "\n## History\n" in old else []
        lines.append(f"- {now:%Y-%m-%d %H:%M} · {_one_line(history)}")
        _write(path, f"---\ntags: [active]\nupdated: {now:%Y-%m-%d}\n---\n# {name}\n\n"
                     f"{summary.strip()}\n\nBack to [[Home]]\n\n## History\n" + "\n".join(lines) + "\n")
        return f"{folder}/{name}"
    except Exception as error:
        _warn(error)
        return None


def _latest(folder, count):
    files = [p for p in folder.glob("*.md")] if folder.is_dir() else []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:count]


def _home(base, now):
    notes = day_notes(base)
    month = f"{now:%Y-%m}"
    links = lambda paths, folder: "\n".join(
        f"- [[{folder}/{p.relative_to(base / folder).with_suffix('').as_posix()}]]" for p in paths) or "- (none yet)"
    _write(base / "Home.md", f"""---
tags: [active]
updated: {now:%Y-%m-%d}
---
# Miles Chase

KrisFlyer miles bot (@jameskoh_miles_bot, James Channel topic 2988). It writes what it did here
and reads it back before each Claude call, so it doesn't repeat tips or re-alert news.

- `Activity/YYYY/MM/` one note per day, one line per event (SGT)
- `Deals/` one note per deal or promo alerted
- `Cards/` one note per card or points programme (balance and expiry history)

## This month · {month}
{links([p for p in notes if p.stem.startswith(month)], "Activity")}

## Latest activity
{links(notes[:7], "Activity")}

## Recent deals
{links(_latest(base / "Deals", 10), "Deals")}

## Cards
{links(sorted((base / "Cards").glob("*.md")), "Cards")}
""")


def _body(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return "\n".join(l for l in text.splitlines() if l.strip() and l.strip() != "Back to [[Home]]")


def memory(folders=(), max_chars=MEMORY_CHARS, days=MEMORY_DAYS):
    """Recent Activity lines (newest first) plus the latest notes in `folders`, capped. Never raises."""
    base = root()
    if not base:
        return ""
    try:
        out = []
        for day in day_notes(base)[:days]:
            events = [l for l in day.read_text(encoding="utf-8", errors="replace").splitlines()
                      if l.startswith("- ")]
            out += [f"{day.stem} {l[2:]}" for l in reversed(events)]
        for folder in folders:
            for path in _latest(base / folder, 5):
                out.append(f"[{folder}] " + " | ".join(_body(path).splitlines()[-6:]))
        text = "\n".join(out)
        return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]
    except Exception as error:
        _warn(error)
        return ""


def alerted_links(days=ALERTED_DAYS):
    """URLs the Activity log shows were already sent. Never raises."""
    base = root()
    if not base:
        return set()
    try:
        found = set()
        for day in day_notes(base)[:days]:
            found.update(URL.findall(day.read_text(encoding="utf-8", errors="replace")))
        return found
    except Exception as error:
        _warn(error)
        return set()


def gist(report, limit=300):
    """A one-line summary of a report for the Activity log: tip lines first, then block titles."""
    lines = [l.strip() for l in str(report).splitlines() if l.strip()]
    tips = [l for l in lines if l.startswith("💡")]
    titles = [b.strip().splitlines()[0] for b in re.split(r"\n\s*\n", str(report)) if b.strip()]
    return _one_line(" / ".join(dict.fromkeys(tips + titles)), limit)
