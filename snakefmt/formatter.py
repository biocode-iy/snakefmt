import re
import textwrap
from ast import parse as ast_parse
from pathlib import Path
from typing import Optional, Union

import black
import toml

from snakefmt import DEFAULT_LINE_LENGTH
from snakefmt.exceptions import (
    InvalidPython,
    InvalidParameterSyntax,
    InvalidBlackConfiguration,
    MalformattedToml,
)
from snakefmt.parser.grammar import SnakeRule
from snakefmt.parser.parser import Parser
from snakefmt.parser.syntax import (
    Parameter,
    ParameterSyntax,
    SingleParam,
    RuleInlineSingleParam,
    TAB,
)
from snakefmt.types import TokenIterator

PathLike = Union[Path, str]
rule_like_formatted = {"rule", "checkpoint"}

triple_quote_matcher = re.compile(r"(\"{3}.*?\"{3})|('{3}.*?'{3})", re.DOTALL)


class Formatter(Parser):
    def __init__(
        self,
        snakefile: TokenIterator,
        line_length: int = DEFAULT_LINE_LENGTH,
        black_config: Optional[PathLike] = None,
    ):
        self._line_length = line_length
        self.result = ""
        self.lagging_comments = ""
        self.first = True

        if black_config is None:
            self.black_mode = black.FileMode(line_length=self.line_length)
        else:
            self.black_mode = self.read_black_config(black_config)

        super().__init__(snakefile)  # Call to parse snakefile

    def read_black_config(self, path: PathLike) -> black.FileMode:
        if not Path(path).is_file():
            raise FileNotFoundError(f"{path} is not a file.")

        try:
            pyproject_toml = toml.load(path)
            config = pyproject_toml.get("tool", {}).get("black", {})
        except toml.TomlDecodeError as error:
            raise MalformattedToml(error)

        if "line_length" not in config:
            config["line_length"] = self.line_length

        try:
            return black.FileMode(**config)
        except TypeError as error:
            raise InvalidBlackConfiguration(error)

    @property
    def line_length(self) -> int:
        return self._line_length

    def get_formatted(self):
        return self.result

    def flush_buffer(
        self, from_python: bool = False, final_flush: bool = False
    ) -> None:
        if len(self.buffer) == 0 or self.buffer.isspace():
            self.result += self.buffer
            self.buffer = ""
            return

        if not from_python:
            trailing_newline = False
            if self.buffer.endswith("\n\n"):
                trailing_newline = True
            formatted = self.run_black_format_str(self.buffer)
            if self.target_indent > 0:
                formatted = textwrap.indent(formatted, TAB * self.target_indent)

            if trailing_newline:
                formatted += "\n"
            self.add_newlines(self.target_indent, formatted, final_flush)
        else:
            formatted = self.buffer.rstrip(TAB)
            self.result += formatted
        self.buffer = ""

    def process_keyword_context(self):
        cur_indent = self.context.cur_indent
        self.add_newlines(cur_indent)
        formatted = (
            f"{TAB * cur_indent}{self.context.keyword_name}:{self.context.comment}\n"
        )
        self.result += formatted

    def process_keyword_param(self, param_context):
        self.add_newlines(param_context.target_indent - 1)
        in_rule = issubclass(param_context.incident_vocab.__class__, SnakeRule)
        self.result += self.format_params(param_context, in_rule)

    def run_black_format_str(self, string: str) -> str:
        try:
            fmted = black.format_str(string, mode=self.black_mode)
        except black.InvalidInput as e:
            raise InvalidPython(
                f"Got error:\n```\n{str(e)}\n```\n" f"while formatting code with black."
            ) from None
        return fmted

    def string_format(self, string: str, target_indent: int) -> str:
        # Only indent non-triple-quoted string portions
        pos = 0
        used_indent = TAB * target_indent
        indented = ""
        for match in re.finditer(triple_quote_matcher, string):
            indented += textwrap.indent(string[pos : match.start()], used_indent)
            match_slice = string[match.start() : match.end()].replace("\t", TAB)
            if match_slice.count("\n") > 0 and target_indent > 0:
                # Note, cannot use 'eval' function as it
                # unescapes escaped special chars like '\n'
                dedented = match_slice.replace('"""', "")
                dedented = f'"""{textwrap.dedent(dedented)}"""'
                indented += textwrap.indent(dedented, used_indent)
            else:
                indented += f"{used_indent}{match_slice}"
            pos = match.end()
        indented += textwrap.indent(string[pos:], used_indent)

        return indented

    def format_param(
        self,
        parameter: Parameter,
        target_indent: int,
        inline_formatting: bool,
        single_param: bool = False,
    ) -> str:
        if inline_formatting:
            target_indent = 0
        comments = f"\n{TAB * target_indent}".join(parameter.comments)
        val = str(parameter)

        try:
            ast_parse(f"param({val})")
        except SyntaxError:
            raise InvalidParameterSyntax(f"{parameter.line_nb}{val}") from None

        if inline_formatting:
            val = val.replace("\n", "")  # collapse strings on multiple lines
        try:
            val = self.run_black_format_str(val)
            val = self.string_format(val, target_indent)
            if parameter.has_a_key():  # Remove space either side of '='
                match_equal = re.match("(.*?) = (.*)", val, re.DOTALL)
                val = f"{match_equal.group(1)}={match_equal.group(2)}"

        except InvalidPython:
            if "**" in val:
                val = val.replace("** ", "**")
            pass

        val = val.strip("\n")
        if single_param:
            result = f"{val}{comments}\n"
        else:
            result = f"{val},{comments}\n"
        return result

    def format_params(self, parameters: ParameterSyntax, in_rule: bool) -> str:
        target_indent = parameters.target_indent
        used_indent = TAB * (target_indent - 1)
        result = f"{used_indent}{parameters.keyword_name}:{parameters.comment}"

        p_class = parameters.__class__
        single_param = issubclass(p_class, SingleParam)
        inline_fmting = single_param
        # Cancel single param formatting if in rule-like context and param not inline
        if in_rule and p_class is not RuleInlineSingleParam:
            inline_fmting = False

        if inline_fmting:
            result += " "
        else:
            result += "\n"

        for elem in parameters.all_params:
            result += self.format_param(
                elem, target_indent, inline_fmting, single_param
            )
        return result

    def add_newlines(
        self, cur_indent: int, formatted_string: str = "", final_flush: bool = False
    ):
        comment_break = 1
        if cur_indent == 0:
            if not self.first:
                self.result += "\n\n"

            if self.lagging_comments != "":
                self.result += self.lagging_comments
                self.lagging_comments = ""

            all_lines = formatted_string.splitlines()
            if len(all_lines) > 0:
                comment_matches = 0
                for line in reversed(all_lines):
                    if len(line) == 0 or line[0] != "#":
                        break
                    comment_matches += 1
                comment_break = len(all_lines) - comment_matches
                if comment_break > 0:
                    self.result += "\n".join(all_lines[:comment_break]).rstrip() + "\n"
                if comment_matches > 0:
                    self.lagging_comments = "\n".join(all_lines[comment_break:]) + "\n"
                    if final_flush:
                        self.result += self.lagging_comments
        else:
            self.result += formatted_string

        if self.first:
            if comment_break != 0:
                self.first = False
