"""Policy image instructions: parsing, validation, and how policies serialize."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tth_types.image_instructions import Heredoc, ImageInstruction, parse_image_instructions
from tth_types.sandbox import (
    IMAGE_DOCKERFILE_MAX_CHARS,
    SandboxPolicy,
    SandboxPolicyRef,
    SandboxPolicyRevision,
)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("FROM debian", "Leave out FROM"),
        ("from debian", "Leave out FROM"),
        ("CMD sleep", "CMD is not allowed"),
        ("ENTRYPOINT sh", "ENTRYPOINT is not allowed"),
        ("HEALTHCHECK NONE", "HEALTHCHECK is not allowed"),
        ("VOLUME /cache", "VOLUME is not allowed"),
        ("EXPOSE 9000", "EXPOSE is not allowed"),
        ('SHELL ["bash", "-c"]', "SHELL is not allowed"),
        ("ONBUILD RUN true", "ONBUILD is not allowed"),
        ("apt-get install g++", "apt-get is not allowed"),
        # A keyword that only upper-cases to an instruction is not one to Docker.
        ("U\u017fER root", "U\u017fER is not allowed"),
        ("RUN --mount=type=cache,target=/root/.cache/uv true", "RUN --mount is not allowed"),
        ("RUN --network=host curl x", "RUN --network is not allowed"),
        ("run --security=insecure true", "RUN --security is not allowed"),
        ("RUN --device=/dev/fuse true", "RUN --device is not allowed"),
        ("RUN --chmod=755 true", "RUN --chmod is not allowed"),
        # Texts that once got a --mount past the scanner.
        ('RUN --mo"unt"=type=cache,target=/x true', 'RUN --mo"unt" is not allowed'),
        ("RUN --mou\\nt=type=cache,target=/x true", "RUN --mou\\nt is not allowed"),
        ("RUN --mo\\\nunt=type=cache,target=/x true", "RUN --mount is not allowed"),
        (
            "RUN echo x'<<EOF'\nRUN --mount=type=cache,target=/x true\nRUN true \\\nEOF",
            "RUN --mount is not allowed",
        ),
        (
            "RUN cat<<ENV\nRUN --mount=type=cache,target=/x true\nRUN echo \\\nENV",
            "RUN --mount is not allowed",
        ),
        ("RUN true \\\u00a0\nRUN --mount=type=cache,target=/x true", "RUN --mount is not allowed"),
        ("RUN", "RUN needs arguments"),
        ("ENV --x=1", "ENV --x is not allowed"),
        ("COPY --foo=1 a b", "COPY --foo is not allowed"),
        ("ADD --from=rust:1 a b", "ADD --from is not allowed"),
        ('COPY --chown="a b" x y', "Write COPY --chown as --chown=value"),
        ("COPY onlyone", "COPY needs a source and a destination"),
        ("COPY <<EOT ${X:-/tmp}\nhi\nEOT", "COPY with a heredoc takes plain paths"),
        ("COPY <<E\\OF /x\nhi\nEOF", "Write a COPY heredoc as <<NAME"),
        ("COPY --chown=$U <<EOT /x\nhi\nEOT", "COPY with a heredoc takes options without"),
        ("RUN true \\", "ends with a line continuation"),
        ("RUN <<EOF\necho", "heredoc EOF is never closed"),
        ("RUN echo \x1b[0m", "control characters"),
        ("RUN echo \x85 x", "control characters"),
        ("RUN true \\\n  && echo hi\nCMD x", "CMD is not allowed"),
    ],
)
def test_policy_rejects_instructions_that_would_change_the_split(text: str, message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        SandboxPolicy(project_root="/project", image_dockerfile=text)


@pytest.mark.parametrize(
    "text",
    [
        (
            "USER root\nRUN apt-get update \\\n"
            "    # a comment inside the continuation\n"
            "    && apt-get install -y g++"
        ),
        # Docker skips blank lines inside a continuation.
        "RUN apt-get install \\\n\n    g++",
        "RUN a\n\\\n\nRUN true",
        "RUN <<EOF\nFROM is just text in a heredoc\nCMD too\nEOF\nENV A=1",
        "RUN <<-'EOT'\n\tFROM x\n\tEOT",
        "RUN python3 - <<'PY' && echo done\nprint(1)\nPY",
        "COPY --from=rust:1 /usr/local/cargo /opt/cargo\nARG X=1\nWORKDIR /tmp\nLABEL a=b",
        "COPY --chmod=644 --chown=agent:agent <<'EOT' <<-EOF /etc/\nx\nEOT\n\ty\n\tEOF",
        'ADD --checksum=sha256:abc https://example.com/x.tgz /opt/\nCOPY ["a b", "/c"]',
        "RUN echo $((1 << 2)) && echo $((1<<n)) && true",
        "RUN printf 'x<<y' && echo \"<<EOF\"",
        'RUN ["sh", "-c", "cat <<EOF"]',
    ],
)
def test_policy_accepts_build_instructions(text: str) -> None:
    assert SandboxPolicy(project_root="/project", image_dockerfile=text).image_dockerfile == text


def test_parser_joins_continuations_and_reads_heredocs_like_docker() -> None:
    text = (
        "run echo a\\\n"
        "  # skipped\n"
        "\n"
        "b\n"
        "RUN cat <<-'EOT' >/etc/motd && cat <<EOF\n"
        "\thello\n"
        "\tEOT\n"
        "world\n"
        "EOF\n"
        'RUN ["echo", "<<EOF"]\n'
        "COPY --link <<EOT /x\n"
        "body\n"
        "EOT\n"
        "ENV A=1 \\\n"
        "  B=2"
    )

    run, heredocs, exec_form, copy, env = parse_image_instructions(text)

    # Joined without a separator, as Docker joins them.
    assert run == ImageInstruction("RUN", "echo ab")
    assert heredocs.heredocs == (
        Heredoc("<<-'EOT'", "EOT", True, ("\thello",), "\tEOT"),
        Heredoc("<<EOF", "EOF", False, ("world",), "EOF"),
    )
    assert heredocs.heredocs[0].content == "hello\n"
    assert exec_form.exec_form == ("echo", "<<EOF")
    assert copy.options == ("--link",) and copy.words == ["<<EOT", "/x"]
    assert copy.heredocs == (Heredoc("<<EOT", "EOT", False, ("body",), "EOT"),)
    assert env == ImageInstruction("ENV", "A=1   B=2")


def test_policy_normalizes_text_so_equal_images_hash_alike() -> None:
    policy = SandboxPolicy(
        project_root="/project", image_dockerfile="\r\n  RUN true\r\nENV A=1\r\n\n"
    )
    assert policy.image_dockerfile == "RUN true\nENV A=1"
    assert SandboxPolicy(project_root="/project", image_dockerfile=" \n\t").image_dockerfile is None
    # Rows stored before the field existed still validate.
    assert SandboxPolicy.model_validate({"project_root": "/project"}).image_dockerfile is None
    with pytest.raises(ValidationError):
        SandboxPolicy(
            project_root="/project", image_dockerfile="#" * (IMAGE_DOCKERFILE_MAX_CHARS + 1)
        )


def test_policy_without_image_text_serializes_without_the_field() -> None:
    """Readers built before the field existed forbid unknown fields, even null ones."""
    plain = SandboxPolicy(project_root="/project")
    custom = SandboxPolicy(project_root="/project", image_dockerfile="RUN true")
    revision = SandboxPolicyRevision(ref=SandboxPolicyRef(id=uuid4(), revision=1), policy=plain)

    assert "image_dockerfile" not in plain.model_dump(mode="json")
    assert "image_dockerfile" not in revision.model_dump(mode="json")["policy"]
    assert '"image_dockerfile"' not in revision.model_dump_json()
    assert custom.model_dump(mode="json")["image_dockerfile"] == "RUN true"
    assert "image_dockerfile" not in custom.model_dump(mode="json", exclude={"image_dockerfile"})
    assert SandboxPolicy.model_validate(custom.model_dump(mode="json")) == custom
