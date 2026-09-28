"""Parse the Dockerfile text a sandbox policy adds on top of a harness image.

TalkToHarnesses never hands this text to Docker as written. It parses it here,
and the proxy renders every accepted instruction again in a canonical form
(``talktoharnesses.remote.custom_images.render``): each instruction on one
line, ``RUN`` as a JSON exec form, and no comments, line continuations or
options this parser did not check. BuildKit therefore builds what this parser
read, even where it would have read the original text differently.

The parser follows Docker's rules for comments, line continuations and
heredocs, so ordinary Dockerfile text means here what it means to Docker.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import cast

# Instructions a policy may add on top of a harness image. Those that would
# change how the split service starts, listens or mounts are not allowed, and
# the built image is verified against the harness image.
IMAGE_INSTRUCTIONS = frozenset({"RUN", "ENV", "ARG", "USER", "WORKDIR", "COPY", "ADD", "LABEL"})
# Options COPY and ADD may take. No other instruction takes any: RUN's
# --mount, --network, --security and --device would reach outside the build.
_OPTIONS = {
    "COPY": frozenset({"from", "chown", "chmod", "link", "parents", "exclude"}),
    "ADD": frozenset({"chown", "chmod", "link", "checksum", "keep-git-dir", "exclude", "unpack"}),
}
_OPTION = re.compile(r"--([a-z][a-z-]*)(?:=[A-Za-z0-9._/:@%+=,~^*?!\[\]{}$-]*)?")
_CONTINUATION = re.compile(r"\\[ \t]*$")
_BLANKS = re.compile(r"[ \t]+")
# A heredoc word, as Docker recognizes it on a RUN line.
_HEREDOC = re.compile(r"[0-9]*<<(-?)([^<]+)")
# COPY and ADD heredocs are rendered again as written, so they stay simple.
_COPY_HEREDOC = re.compile(r"<<(-?)(?:([A-Za-z0-9_.-]+)|'([A-Za-z0-9_.-]+)'|\"([A-Za-z0-9_.-]+)\")")
_PLAIN_WORD = re.compile(r"[A-Za-z0-9._/:@%+=,~^*?!\[\]{}-]+")


@dataclass(frozen=True)
class Heredoc:
    """A heredoc: the lines following an instruction up to its terminator."""

    word: str  # as written on the instruction line, e.g. <<-'EOF'
    name: str  # the terminator
    chomp: bool  # <<-: leading tabs are ignored
    lines: tuple[str, ...]  # the body, as written
    end: str  # the terminator line, as written

    @property
    def content(self) -> str:
        lines = [line.lstrip("\t") for line in self.lines] if self.chomp else self.lines
        return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class ImageInstruction:
    keyword: str  # upper case, one of IMAGE_INSTRUCTIONS
    arguments: str  # the rest of the instruction line, continuations joined
    options: tuple[str, ...] = ()  # COPY and ADD options such as --chown=1000
    exec_form: tuple[str, ...] | None = None  # the arguments, when a JSON array
    heredocs: tuple[Heredoc, ...] = ()

    @property
    def words(self) -> list[str]:
        """The arguments split at blanks, as Docker splits COPY and ADD sources."""
        return _BLANKS.split(self.arguments)


def parse_image_instructions(text: str) -> tuple[ImageInstruction, ...]:
    """The instructions in ``text``; raises ``ValueError`` for text a harness image must not run."""
    if any(unicodedata.category(char) == "Cc" and char not in "\t\n" for char in text):
        raise ValueError("Image instructions must not contain control characters.")
    lines = text.split("\n")
    instructions: list[ImageInstruction] = []
    index = 0
    while index < len(lines):
        first = lines[index].lstrip(" \t")
        index += 1
        if not first or first.startswith("#"):
            continue
        logical, continued = _continuation(first)
        while continued:
            if index == len(lines):
                raise ValueError("The last image instruction ends with a line continuation.")
            line = lines[index]
            index += 1
            # Like Docker, skip comments and blank lines inside a continuation
            # and join the rest without a separator.
            if not line.strip(" \t") or line.lstrip(" \t").startswith("#"):
                continue
            piece, continued = _continuation(line)
            logical += piece
        instruction, index = _instruction(logical.strip(" \t"), lines, index)
        instructions.append(instruction)
    return tuple(instructions)


def _continuation(line: str) -> tuple[str, bool]:
    trimmed = _CONTINUATION.sub("", line)
    return trimmed, trimmed != line


def _instruction(logical: str, lines: list[str], index: int) -> tuple[ImageInstruction, int]:
    word, *rest = _BLANKS.split(logical, maxsplit=1)
    arguments = rest[0] if rest else ""
    keyword = word.upper() if word.isascii() else word
    if keyword == "FROM":
        raise ValueError("Leave out FROM: the instructions are applied to each harness image.")
    if keyword not in IMAGE_INSTRUCTIONS:
        raise ValueError(f"{word} is not allowed in image instructions.")
    options, arguments = _options(keyword, arguments)
    if not arguments:
        raise ValueError(f"{keyword} needs arguments.")
    heredocs: list[Heredoc] = []
    exec_form = _json_array(arguments) if keyword in {"RUN", "COPY", "ADD"} else None
    if keyword == "RUN" and exec_form is None and "<<" in arguments:
        for raw, _ in _words(arguments) or ():
            match = _HEREDOC.fullmatch(raw)
            name = _words(match.group(2)) if match else None
            if match and name and len(name) == 1 and name[0][1]:
                heredoc, index = _heredoc(raw, name[0][1], match.group(1) == "-", lines, index)
                heredocs.append(heredoc)
    elif keyword in {"COPY", "ADD"} and exec_form is None and "<<" in arguments:
        heredocs, index = _copy_heredocs(keyword, options, arguments, lines, index)
    if keyword in {"COPY", "ADD"} and len(exec_form or _BLANKS.split(arguments)) < 2:
        raise ValueError(f"{keyword} needs a source and a destination.")
    return ImageInstruction(keyword, arguments, options, exec_form, tuple(heredocs)), index


def _options(keyword: str, arguments: str) -> tuple[tuple[str, ...], str]:
    options: list[str] = []
    while arguments.startswith("--"):
        token, *rest = _BLANKS.split(arguments, maxsplit=1)
        arguments = rest[0] if rest else ""
        name = token.split("=", 1)[0]
        if name[2:] not in _OPTIONS.get(keyword, ()):
            raise ValueError(f"{keyword} {name} is not allowed.")
        if not _OPTION.fullmatch(token):
            raise ValueError(
                f"Write {keyword} {name} as {name}=value, without quotes, spaces or backslashes."
            )
        options.append(token)
    return tuple(options), arguments


def _json_array(arguments: str) -> tuple[str, ...] | None:
    """The arguments when Docker reads them as a JSON exec form."""
    if not arguments.startswith("["):
        return None
    try:
        value: object = json.loads(arguments)
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    items = [item for item in cast("list[object]", value) if isinstance(item, str)]
    if not items or len(items) != len(cast("list[object]", value)):
        return None
    return tuple(items)


def _words(text: str) -> list[tuple[str, str]] | None:
    """Shell words of ``text`` as (as written, unquoted); None for an unterminated quote.

    Like Docker's heredoc detection this handles quotes and backslashes but no
    expansions.
    """
    words: list[tuple[str, str]] = []
    raw = value = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t":
            if raw:
                words.append((raw, value))
                raw = value = ""
            index += 1
        elif char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                return None
            raw += text[index : end + 1]
            value += text[index + 1 : end]
            index = end + 1
        elif char == '"':
            end = index + 1
            quoted = ""
            while end < len(text) and text[end] != '"':
                if text[end] == "\\" and end + 1 < len(text) and text[end + 1] in '"$\\':
                    end += 1
                quoted += text[end]
                end += 1
            if end == len(text):
                return None
            raw += text[index : end + 1]
            value += quoted
            index = end + 1
        elif char == "\\" and index + 1 < len(text):
            raw += text[index : index + 2]
            value += text[index + 1]
            index += 2
        else:
            raw += char
            value += char
            index += 1
    if raw:
        words.append((raw, value))
    return words


def _heredoc(
    word: str, name: str, chomp: bool, lines: list[str], index: int
) -> tuple[Heredoc, int]:
    body: list[str] = []
    while index < len(lines):
        line = lines[index]
        index += 1
        if (line.lstrip("\t") if chomp else line) == name:
            return Heredoc(word, name, chomp, tuple(body), line), index
        body.append(line)
    raise ValueError(f"The heredoc {name} is never closed.")


def _copy_heredocs(
    keyword: str, options: tuple[str, ...], arguments: str, lines: list[str], index: int
) -> tuple[list[Heredoc], int]:
    heredocs: list[Heredoc] = []
    for word in _BLANKS.split(arguments):
        if "<<" not in word:
            if not _PLAIN_WORD.fullmatch(word):
                raise ValueError(
                    f"{keyword} with a heredoc takes plain paths, without quotes, "
                    "backslashes or variables."
                )
            continue
        match = _COPY_HEREDOC.fullmatch(word)
        if match is None:
            raise ValueError(f"Write a {keyword} heredoc as <<NAME, <<'NAME' or <<\"NAME\".")
        name = next(group for group in match.groups()[1:] if group)
        heredoc, index = _heredoc(word, name, match.group(1) == "-", lines, index)
        heredocs.append(heredoc)
    if any("$" in option for option in options):
        raise ValueError(f"{keyword} with a heredoc takes options without variables.")
    return heredocs, index
