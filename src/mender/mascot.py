"""Mender's mascot — small ASCII face + personality lines.

Used by the heartbeat / investigate CLI to print a wake-up sequence at
the start of a cycle (title → sleeping → awake → observing) and a
result beat at the end. Pure cosmetics; no behavior depends on it.

Lines are hardcoded so multiple takes during video capture produce
identical output.

Faces are 3 lines tall, 5 chars wide. Designed to render cleanly in
common monospace fonts (SF Mono, Menlo, JetBrains Mono).
"""

from __future__ import annotations

import time

from rich.console import Console

# ----------------------------------------------------------------------------
# Title block (printed once at the top of each cycle)
# ----------------------------------------------------------------------------

_TITLE_LINES = (
    "  ╭──────────╮",
    "  │  Mender  │",
    "  ╰──────────╯",
    "   catches the cracks. mends them.",
)


# ----------------------------------------------------------------------------
# Faces
# ----------------------------------------------------------------------------

_FACE_SLEEPING  = ("╭───╮", "│- -│", "╰───╯")
_FACE_AWAKE     = ("╭───╮", "│◉◡◉│", "╰───╯")
_FACE_OBSERVING = ("╭───╮", "│◉ ◉│", "╰───╯")
_FACE_CONCERNED = ("╭───╮", "│◉_◉│", "╰───╯")
_FACE_HAPPY     = ("╭───╮", "│◉◡◉│", "╰───╯")  # alias for awake; pleasant outcome


# ----------------------------------------------------------------------------
# Lines (hardcoded — no rotation)
# ----------------------------------------------------------------------------

LINE_SLEEPING   = "zZz..."
LINE_WAKE       = "I'm awake."
LINE_OBSERVING  = "Let me take a look."
LINE_OK         = "All clear. Catch you in 15."
LINE_REGRESSION = "Hmm. Something's broken. Drafting a fix."
LINE_WATCHING   = "Trend's wobbling. I'll keep an eye on it."
LINE_APPLIED    = "Cracks mended. Back to sleep."
LINE_DISCARDED  = "Got it. Standing down."


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------

def _print_face(
    console: Console,
    face: tuple[str, str, str],
    line: str,
    *,
    face_color: str = "cyan",
    line_color: str | None = None,
    pause_after: float = 0.0,
) -> None:
    """Print a 3-line face with the line aligned to the middle row."""
    line_style = line_color or "bold white"
    console.print(f"  [{face_color}]{face[0]}[/]")
    console.print(f"  [{face_color}]{face[1]}[/]  [{line_style}]{line}[/]")
    console.print(f"  [{face_color}]{face[2]}[/]")
    if pause_after > 0:
        # Force flush so the face is definitively on screen before the
        # sleep — rich.Console buffers, which would otherwise queue the
        # next frame and collapse the visible pause.
        try:
            console.file.flush()
        except (AttributeError, OSError):
            pass
        time.sleep(pause_after)


def _print_title(console: Console) -> None:
    """Print the Mender title block at the top of the cycle."""
    for i, line in enumerate(_TITLE_LINES):
        # First three lines (the box) in cyan, tagline in dim.
        if i < 3:
            console.print(f"[cyan]{line}[/]")
        else:
            console.print(f"[dim]{line}[/]")
    console.print()


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def wake_up(
    console: Console,
    *,
    asleep_beat: bool = True,
    clear_screen: bool = True,
) -> None:
    """Print the full pre-cycle sequence: title → sleeping → awake → observing.

    Parameters:
        asleep_beat: when True, shows the sleeping face with a 15s pause
            before waking up. Set False to skip the sleep frame (e.g.
            inside investigate where the cycle is already running).
        clear_screen: when True (default), the terminal is cleared first
            (visible buffer + scrollback) so the mascot appears at the
            top of an empty screen. Keeps Scene 3 capture clean from any
            startup warnings or init logs.
    """
    if clear_screen:
        # ANSI: 2J clear screen, 3J clear scrollback (Mac Terminal/iTerm),
        # H move cursor to home position.
        try:
            console.file.write("\033[2J\033[3J\033[H")
            console.file.flush()
        except (AttributeError, OSError):
            pass

    _print_title(console)

    if asleep_beat:
        _print_face(
            console, _FACE_SLEEPING, LINE_SLEEPING,
            face_color="dim cyan", line_color="dim",
            pause_after=5.0,
        )
        console.print()

    _print_face(
        console, _FACE_AWAKE, LINE_WAKE,
        face_color="cyan", line_color="bold white",
        pause_after=5.0,
    )
    console.print()

    _print_face(
        console, _FACE_OBSERVING, LINE_OBSERVING,
        face_color="cyan", line_color="bold white",
        pause_after=5.0,
    )
    console.print()


def report(console: Console, status: str) -> None:
    """Print the closer based on the cycle's reported status.

    `status` should be one of: 'ok', 'watching', 'regression'. Anything
    else is treated as 'ok'.
    """
    status = (status or "").strip().lower()
    console.print()
    if status == "regression":
        _print_face(
            console, _FACE_CONCERNED, LINE_REGRESSION,
            face_color="yellow", line_color="bold yellow",
        )
    elif status == "watching":
        _print_face(
            console, _FACE_CONCERNED, LINE_WATCHING,
            face_color="yellow", line_color="yellow",
        )
    else:  # 'ok' or unknown
        _print_face(
            console, _FACE_HAPPY, LINE_OK,
            face_color="green", line_color="bold green",
        )


def slack_block(face: tuple[str, str, str], line: str) -> str:
    """Render a mascot block as a Slack monospace code block.

    Slack preserves whitespace inside triple-backtick blocks, so the
    box-drawing characters and spacing line up correctly. Use as the
    `text` of a Slack mrkdwn section, or append to an existing message.
    """
    return (
        "```\n"
        f"  {face[0]}\n"
        f"  {face[1]}  {line}\n"
        f"  {face[2]}\n"
        "```"
    )


def slack_block_for_action(action: str) -> str:
    """Convenience: pick the right face + line for a Slack post-action message.

    `action` should be one of: 'applied', 'discarded'. Anything else
    raises ValueError.
    """
    if action == "applied":
        return slack_block(_FACE_HAPPY, LINE_APPLIED)
    if action == "discarded":
        return slack_block(_FACE_CONCERNED, LINE_DISCARDED)
    raise ValueError(f"unknown action {action!r}")


def parse_status(final_text: str) -> str:
    """Extract the `[status]` line from the agent's final response.

    The heartbeat agent is instructed to emit:
        [status]  ok | watching | regression
    Returns the status keyword, or 'ok' if it can't find one.
    """
    if not final_text:
        return "ok"
    for raw in final_text.splitlines():
        line = raw.strip().lower()
        if line.startswith("[status]"):
            rest = line.removeprefix("[status]").strip()
            for token in ("regression", "watching", "ok"):
                if token in rest:
                    return token
    return "ok"
