import abc
import io
import re
from configparser import ConfigParser
from typing import Any, BinaryIO, List, Optional

from .parsers import (
    dump_setupcfg_requirements,
    parse_setupcfg_requirements,
    parser_to_pretty_output,
    pylintrc_parser,
    requirement_name,
    setup_cfg_parser,
)
from .template import TemplateMeta


def _ensure_section(parser: ConfigParser, section: str) -> None:
    if not parser.has_section(section):
        parser.add_section(section)


class Strategy(abc.ABC):
    def __init__(
        self,
        *,
        target_contents: Optional[BinaryIO],
        template_contents: BinaryIO,
        template_meta: TemplateMeta,
    ) -> None:
        self.target_contents = target_contents
        self.template_contents = template_contents
        self.template_meta = template_meta

    @abc.abstractmethod
    def apply(self) -> BinaryIO:
        pass


class Overwrite(Strategy):
    """A simple strategy which always overwrites the target files
    with the ones from the template.
    """

    def apply(self) -> BinaryIO:
        return self.template_contents


class IfMissing(Strategy):
    """A strategy which writes the file from the template only
    if it doesn't exist in target.
    """

    def apply(self) -> BinaryIO:
        if self.target_contents is None:
            return self.template_contents
        else:
            return self.target_contents


class SortedUniqueLines(Strategy):
    """A strategy which combines both template and target files,
    sorts the combined lines and keeps only unique ones.
    """

    def apply(self) -> BinaryIO:
        out_lines = self.template_contents.read().decode().splitlines()
        if self.target_contents is not None:
            out_lines.extend(self.target_contents.read().decode().splitlines())

        # Keep unique lines and sort them.
        #
        # Note that `set` is not guaranteed to preserve the original
        # order, so we need to compare by both casefolded str and
        # the original to ensure the stable order for the same strings
        # written in different cases.
        out_lines = sorted(set(out_lines), key=lambda s: (s.casefold(), s))

        out_lines = [line for line in out_lines if line]
        out_lines.append("")  # trailing newline

        return io.BytesIO("\n".join(out_lines).encode())


class TemplateHash(Strategy):
    """A strategy which appends to the target file a git commit hash of
    the template being applied; and the subsequent applications of
    the same template for this file are ignored.

    This strategy is useful when a file needs to be different from
    the template, yet it should be resynced on template updates.
    """

    line_comment_start = "#"

    def comment(self) -> str:
        comment_lines = [f"Generated by https://github.com/rambler-digital-solutions/scaraplate"]
        if self.template_meta.is_git_dirty:
            comment_lines.append(f"From (dirty) {self.template_meta.commit_url}")
        else:
            comment_lines.append(f"From {self.template_meta.commit_url}")

        return "".join(f"{self.line_comment_start} {line}\n" for line in comment_lines)

    def apply(self) -> BinaryIO:
        comment = self.comment().encode("ascii")
        if self.target_contents is not None:
            target_text = self.target_contents.read()
            if comment in target_text and not self.template_meta.is_git_dirty:
                # Hash hasn't changed -- keep the target.
                self.target_contents.seek(0)
                return self.target_contents

        out_bytes = self.template_contents.read()
        out_bytes += b"\n" + comment
        return io.BytesIO(out_bytes)


class PythonTemplateHash(TemplateHash):
    """TemplateHash strategy which takes Python linters into account:
    the long lines of the appended comment are suffixed with `# noqa`.
    """

    line_length = 87

    def comment(self) -> str:
        comment = super().comment()
        comment_lines = comment.split("\n")
        comment_lines = [self._maybe_add_noqa(line) for line in comment_lines]
        return "\n".join(comment_lines)

    def _maybe_add_noqa(self, line: str) -> str:
        if len(line) >= self.line_length:
            return f"{line}  # noqa"
        return line


class GroovyTemplateHash(TemplateHash):
    """TemplateHash strategy for Groovy files. Actually, it may be applied to any
    languages which uses ``//``-style commentaries, but so far Groovy is the only user
    of this.
    """

    line_comment_start = "//"


class PylintrcMerge(Strategy):
    """A strategy which merges `.pylintrc` between a template
    and the target project.

    The resulting `.pylintrc` is the one from the template with
    the following modifications:
    - Comments are stripped
    - INI file is reformatted (whitespaces are cleaned, sections
      and values are sorted)
    - `ignored-*` keys of the `[TYPECHECK]` section are taken from
      the target `.pylintrc`.
    """

    def apply(self) -> BinaryIO:
        template_parser = pylintrc_parser(
            self.template_contents, source=".pylintrc.template"
        )

        if self.target_contents is not None:
            target_parser = pylintrc_parser(
                self.target_contents, source=".pylintrc.target"
            )
            self._maybe_preserve_key(
                template_parser, target_parser, "TYPECHECK", "ignored-modules"
            )
            self._maybe_preserve_key(
                template_parser, target_parser, "TYPECHECK", "ignored-classes"
            )

        return parser_to_pretty_output(template_parser)

    def _maybe_preserve_key(
        self,
        template_parser: ConfigParser,
        target_parser: ConfigParser,
        section: str,
        key: str,
    ) -> None:
        try:
            target = target_parser[section][key]
        except KeyError:
            # No such section/value in target -- keep the one that is
            # in the template.
            return
        else:
            _ensure_section(template_parser, section)
            template_parser[section][key] = target


class SetupcfgMerge(Strategy):
    def apply(self) -> BinaryIO:
        template_parser = setup_cfg_parser(
            self.template_contents, source="setup.cfg.template"
        )

        target_parser = None

        if self.target_contents is not None:
            target_parser = setup_cfg_parser(
                self.target_contents, source="setup.cfg.target"
            )

            self._maybe_preserve_sections(
                template_parser,
                target_parser,
                # A non-standard section
                re.compile("^freebsd$"),
            )

            self._maybe_preserve_sections(
                template_parser, target_parser, re.compile("^mypy-")
            )

            self._maybe_preserve_sections(
                template_parser, target_parser, re.compile("^options.data_files$")
            )

            self._maybe_preserve_sections(
                template_parser, target_parser, re.compile("^options.entry_points$")
            )

            self._maybe_preserve_sections(
                template_parser,
                target_parser,
                re.compile("^options.extras_require$"),
                ignore_keys_pattern=re.compile("^develop$"),
            )

            self._maybe_preserve_key(
                template_parser, target_parser, "tool:pytest", "testpaths"
            )

            # TODO verify if this is still relevant:
            self._maybe_preserve_key(
                template_parser, target_parser, "build", "executable"
            )

        self._merge_requirements(
            template_parser, target_parser, "options.extras_require", "develop"
        )

        self._merge_requirements(
            template_parser, target_parser, "options", "install_requires"
        )

        return parser_to_pretty_output(template_parser)

    def _maybe_preserve_sections(
        self,
        template_parser: ConfigParser,
        target_parser: ConfigParser,
        sections_pattern: Any,  # re.Pattern since py3.7
        ignore_keys_pattern: Any = None,
    ) -> None:
        for section in target_parser.sections():  # default section is ignored
            if sections_pattern.match(section):
                section_data = dict(target_parser[section])

                if ignore_keys_pattern is not None:
                    for key, value in template_parser[section].items():
                        if ignore_keys_pattern.match(key):
                            section_data[key] = value

                template_parser[section] = section_data

    def _merge_requirements(
        self,
        template_parser: ConfigParser,
        target_parser: Optional[ConfigParser],
        section: str,
        key: str,
    ) -> None:
        "Merge in the requirements from template to the target."

        template_requirements = self._parse_requirements(template_parser, section, key)
        if target_parser is not None:
            target_requirements = self._parse_requirements(target_parser, section, key)
        else:
            target_requirements = []

        def normalize_requirement(requirement):
            return requirement_name(requirement).lower()

        existing_requirement_names = set(
            map(normalize_requirement, target_requirements)
        )
        wanted_requirements = target_requirements

        for requirement in template_requirements:
            name = normalize_requirement(requirement)
            if name not in existing_requirement_names:
                wanted_requirements.append(requirement)

        wanted_requirements = sorted(wanted_requirements, key=str.casefold)

        _ensure_section(template_parser, section)
        template_parser[section][key] = dump_setupcfg_requirements(wanted_requirements)

    def _parse_requirements(
        self, parser: ConfigParser, section: str, key: str
    ) -> List[str]:
        try:
            requirements = parser[section][key]
        except KeyError:
            return []

        return parse_setupcfg_requirements(requirements)

    def _maybe_preserve_key(
        self,
        template_parser: ConfigParser,
        target_parser: ConfigParser,
        section: str,
        key: str,
    ) -> None:
        try:
            target = target_parser[section][key]
        except KeyError:
            # No such section/value in target -- keep the one that is
            # in the template.
            return
        else:
            _ensure_section(template_parser, section)
            template_parser[section][key] = target
